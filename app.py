from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import sendgrid
from sendgrid.helpers.mail import Mail
import openai
import os
from dotenv import load_dotenv
import boto3
from flask import jsonify, make_response
from botocore.exceptions import ClientError

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Replace with a secure key in production

load_dotenv()
# Use the new OpenAI client
openai_client = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

def init_db():
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()

    # Create teams table
    c.execute('''CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                )''')

    # Update users to include a team_id
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    team_id INTEGER,
                    FOREIGN KEY (team_id) REFERENCES teams(id)
                )''')

    # Update todos to associate with a team (instead of a user)
    c.execute('''CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    team_id INTEGER,
                    api_response TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (team_id) REFERENCES teams(id)
                )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS invitations
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              team_id INTEGER NOT NULL,
              token TEXT NOT NULL UNIQUE,
              FOREIGN KEY (team_id) REFERENCES teams(id))''')

    conn.commit()
    conn.close()

# Load environment variables if needed
from dotenv import load_dotenv
load_dotenv()

SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY')
FROM_EMAIL = os.getenv('FROM_EMAIL')

import smtplib
from email.mime.text import MIMEText

def send_invitation_email(recipient_email, team_name, invite_link):
    try:
        sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

        message = Mail(
            from_email=FROM_EMAIL,
            to_emails=recipient_email,
            subject=f"You have been invited to join the team: {team_name}",
           html_content=f"""
                        <p>Hello,</p>
                        <p>You have been invited to join the team <strong>{team_name}</strong>.</p>
                        <p><a href="{invite_link}">Click here</a> to sign up and join the team.</p>
                            """
        )
        response = sg.send(message)

        if response.status_code >= 200 and response.status_code < 300:
            print("Email sent successfully.")
            return True
        else:
            print(f"Failed to send email. Status code: {response.status_code}")
            return False

    except Exception as e:
        print(f"Error sending email via SendGrid: {e}")
        return False

def get_team_name(team_id):
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()
    c.execute("SELECT name FROM teams WHERE id=?", (team_id,))
    team = c.fetchone()
    conn.close()
    return team[0] if team else "No team"

# after your other imports
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET  = os.getenv("AWS_BUCKET_NAME")

s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)

@app.route('/upload', methods=['GET'])
def upload_form():
    # Renders a page with a file input
    return render_template('upload.html')

@app.route('/sign-s3')
def sign_s3():
    file_name = request.args.get('file_name')
    file_type = request.args.get('file_type')
    if not file_name or not file_type:
        return jsonify(error="Missing file_name or file_type"), 400

    # you can namespace your "folders" here if you like:
    key = f"uploads/{file_name}"

    try:
        post_data = s3_client.generate_presigned_post(
            Bucket=S3_BUCKET,
            Key=key,
            Fields={
                "acl": "public-read",
                "Content-Type": file_type
            },
            Conditions=[
                {"acl": "public-read"},
                {"Content-Type": file_type},
                ["content-length-range", 0, 10 * 1024 * 1024]  # up to 10 MB
            ],
            ExpiresIn=3600
        )
    except ClientError as e:
        return jsonify(error=str(e)), 500

    # post_data is a dict with { "url": ..., "fields": { ... } }
    return make_response(jsonify(post_data), 200)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    invite_token = request.args.get('invite_token')

    detected_team_id = None
    detected_team_name = None

    if invite_token:
        conn = sqlite3.connect('todo.db')
        c = conn.cursor()
        c.execute('''
            SELECT teams.id, teams.name FROM invitations
            JOIN teams ON invitations.team_id = teams.id
            WHERE invitations.token=?
        ''', (invite_token,))
        row = c.fetchone()
        conn.close()
        if row:
            detected_team_id = row[0]
            detected_team_name = row[1]

    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        new_team_name = request.form.get('new_team')
        team_id = request.form.get('team_id')

        conn = sqlite3.connect('todo.db')
        c = conn.cursor()

        try:
            # 🚀 If came from invite, override any form team_id
            if detected_team_id:
                team_id_to_use = detected_team_id
            elif new_team_name:
                # 🚀 User created a new team
                c.execute("INSERT INTO teams (name) VALUES (?)", (new_team_name,))
                conn.commit()
                team_id_to_use = c.lastrowid
            elif team_id:
                # 🚀 User selected an existing team
                team_id_to_use = team_id
            else:
                # 🚀 No team assigned
                team_id_to_use = None

            # Create the user
            hashed_password = generate_password_hash(password)
            c.execute(
                "INSERT INTO users (email, password, team_id) VALUES (?, ?, ?)",
                (email, hashed_password, team_id_to_use)
            )
            conn.commit()

            # Fetch and login
            c.execute("SELECT * FROM users WHERE email=?", (email,))
            user = c.fetchone()

            if user:
                session['user_id'] = user[0]
                session['email'] = user[1]
                session['team_id'] = user[3] if len(user) > 3 else None
                session['team_name'] = get_team_name(session['team_id']) if session['team_id'] else "No team"
                flash('Signup successful!')
                return redirect(url_for('index'))
            else:
                flash('Signup failed, try again.')
                return redirect(url_for('signup'))

        except sqlite3.IntegrityError:
            flash('Email already exists')
            return redirect(url_for('signup'))
        finally:
            conn.close()

    # If GET request
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()
    c.execute("SELECT id, name FROM teams")
    teams = c.fetchall()
    conn.close()

    return render_template('signup.html', teams=teams, detected_team_name=detected_team_name)




@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        conn = sqlite3.connect('todo.db')
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email=?", (email,))
        user = c.fetchone()

        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['email'] = user[1]
            session['team_id'] = user[3]   # Save team_id

            # 🔥 Fetch the team name:
            c.execute("SELECT name FROM teams WHERE id=?", (user[3],))
            team = c.fetchone()
            if team:
                session['team_name'] = team[0]
            else:
                session['team_name'] = "No team"  # fallback if team missing

            conn.close()

            flash('Login successful')
            return redirect(url_for('index'))
        else:
            conn.close()
            flash('Invalid email or password')
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('email', None)
    flash('You have been logged out')
    return redirect(url_for('login'))

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    team_id = session.get('team_id')

    conn = sqlite3.connect('todo.db')
    c = conn.cursor()

    if team_id:
        c.execute('''
            SELECT todos.id, todos.task, todos.status, todos.user_id, todos.api_response 
            FROM todos
            JOIN users ON todos.user_id = users.id
            WHERE users.team_id=?
            ORDER BY todos.id DESC
        ''', (team_id,))
    else:
        c.execute('''
            SELECT id, task, status, user_id, api_response 
            FROM todos 
            WHERE user_id=?
            ORDER BY id DESC
        ''', (user_id,))

    tasks = c.fetchall()
    conn.close()

    return render_template('index.html', tasks=tasks, team_name=session.get('team_name', 'No Team'))



@app.route('/add', methods=['POST'])
def add_task():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    task = request.form['task']
    if task:
        user_id = session['user_id']
        team_id = session['team_id']
        api_response = ""

        # Send the task to the OpenAI API
        try:
            response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are an assistant that provides ideas, links, and books related to a given task."},
                    {"role": "user", "content": f"Please provide ideas, links, and books related to: {task}"}
                ],
                max_tokens=200,
                temperature=0.7
            )
            api_response = response.choices[0].message.content.strip()
        except Exception as e:
            api_response = "Error retrieving information. Please try again later."
            print(f"OpenAI API error: {e}")

        conn = sqlite3.connect('todo.db')
        c = conn.cursor()
        # Check if the task already exists for this user
        c.execute("SELECT id FROM todos WHERE task=? AND user_id=?", (task, user_id))
        if c.fetchone() is None:
            c.execute(
                "INSERT INTO todos (task, status, user_id, team_id, api_response) VALUES (?, ?, ?, ?, ?)",
                (task, 'Pending', user_id, team_id, api_response)
            )
            conn.commit()
        conn.close()
    return redirect(url_for('index'))


@app.route('/delete/<int:task_id>')
def delete_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()
    # Ensure that the task belongs to the current user
    c.execute("DELETE FROM todos WHERE id=? AND user_id=?", (task_id, user_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/complete/<int:task_id>')
def complete_task(task_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()
    # Ensure that the task belongs to the current user
    c.execute("UPDATE todos SET status='Completed' WHERE id=? AND user_id=?", (task_id, user_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

import secrets

@app.route('/invite', methods=['GET', 'POST'])
def invite():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        receiver_email = request.form['email']
        team_id = session['team_id']
        team_name = session['team_name']

        # 🔥 Generate a secure random token
        token = secrets.token_urlsafe(16)

        # 🔥 Save token and team_id into the invitations table
        conn = sqlite3.connect('todo.db')
        c = conn.cursor()
        c.execute("INSERT INTO invitations (team_id, token) VALUES (?, ?)", (team_id, token))
        conn.commit()
        conn.close()

        # 🔥 Build invite link
        invite_link = url_for('signup', invite_token=token, _external=True)

        # 🔥 Send email with invite link
        send_invitation_email(receiver_email, team_name, invite_link)

        flash('Invitation sent!')
        return redirect(url_for('index'))

    return render_template('invite.html')

@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if 'user_id' not in session:
        flash('Please login first.')
        return redirect(url_for('login'))
    
    conn = sqlite3.connect('todo.db')
    c = conn.cursor()
    
    # Safety: only allow users to delete themselves OR admins (if you have roles later)
    if user_id != session['user_id']:
        flash('You can only delete your own account.')
        conn.close()
        return redirect(url_for('index'))

    # Delete the user's todos first (to maintain database integrity)
    c.execute('DELETE FROM todos WHERE user_id=?', (user_id,))
    c.execute('DELETE FROM users WHERE id=?', (user_id,))
    conn.commit()
    conn.close()

    # Logout after account deletion
    session.clear()
    flash('Your account has been deleted successfully.')
    return redirect(url_for('signup'))


if __name__ == '__main__':
    init_db() 
    app.run(debug=True)
