import streamlit as st
import sqlite3
import hashlib
import pandas as pd
import os
import datetime
from io import BytesIO

DB_FILE = "rent.db"

# -----------------------------
# Database Setup
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
        )
    """)

    # Tenants table
    c.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            monthly_rent REAL NOT NULL,
            rental_address TEXT,
            original_address TEXT
        )
    """)

    # Transactions table
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        )
    """)

    # Create default admin if not exists
    c.execute("SELECT * FROM users WHERE username=?", ("admin",))
    if not c.fetchone():
        admin_pass = hashlib.sha256("admin123".encode()).hexdigest()
        c.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                  ("admin", admin_pass, "admin"))
    conn.commit()
    conn.close()

# -----------------------------
# Utility Functions
# -----------------------------
def get_connection():
    return sqlite3.connect(DB_FILE)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_user(username, password):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username=? AND password_hash=?", 
              (username, hash_password(password)))
    user = c.fetchone()
    conn.close()
    return user

def get_tenants():
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM tenants", conn)
    conn.close()
    return df

def get_transactions():
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM transactions", conn)
    conn.close()
    return df

def calculate_balance(tenant_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT monthly_rent FROM tenants WHERE id=?", (tenant_id,))
    rent = c.fetchone()[0]
    c.execute("SELECT SUM(amount) FROM transactions WHERE tenant_id=?", (tenant_id,))
    paid = c.fetchone()[0] or 0
    # Months since tenant added
    c.execute("SELECT id FROM tenants WHERE id=?", (tenant_id,))
    if not c.fetchone():
        return 0
    months = (datetime.date.today().year - 2020) * 12 + datetime.date.today().month  # simplification
    expected = months * rent
    conn.close()
    return paid - expected

# -----------------------------
# Pages
# -----------------------------
def page_login():
    st.title("🏠 Rent Collection System - Login")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if st.button("Login"):
        user = login_user(username, password)
        if user:
            st.session_state.logged_in = True
            st.session_state.username = user[1]
            st.session_state.role = user[3]
            st.rerun()
        else:
            st.error("Invalid username or password")

def page_tenant_management():
    st.title("👥 Tenant Management")

    tab1, tab2 = st.tabs(["Add Tenant", "Tenant List"])

    with tab1:
        name = st.text_input("Tenant Name")
        rent = st.number_input("Monthly Rent", min_value=0.0, step=100.0)
        rental_address = st.text_area("Rental Property Address")
        original_address = st.text_area("Original Address")
        if st.button("Add Tenant"):
            conn = get_connection()
            c = conn.cursor()
            c.execute("INSERT INTO tenants (name, monthly_rent, rental_address, original_address) VALUES (?, ?, ?, ?)",
                      (name, rent, rental_address, original_address))
            conn.commit()
            conn.close()
            st.success("Tenant added successfully!")

    with tab2:
        tenants = get_tenants()
        st.dataframe(tenants)
        if st.button("Download Tenant List (Excel)"):
            towrite = BytesIO()
            tenants.to_excel(towrite, index=False)
            towrite.seek(0)
            st.download_button("Download Tenant List", data=towrite, file_name="tenants.xlsx")

def page_rent_collection():
    st.title("💰 Rent Collection")

    tenants = get_tenants()
    tenant_names = {row['name']: row['id'] for _, row in tenants.iterrows()}

    tenant_name = st.selectbox("Select Tenant", list(tenant_names.keys()))
    amount = st.number_input("Amount Paid", min_value=0.0, step=100.0)

    if st.button("Record Payment"):
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT INTO transactions (tenant_id, date, amount) VALUES (?, ?, ?)",
                  (tenant_names[tenant_name], str(datetime.date.today()), amount))
        conn.commit()
        conn.close()
        st.success(f"Payment of {amount} recorded for {tenant_name}")

def page_reports():
    st.title("📊 Reports")

    tenants = get_tenants()
    rows = []
    for _, t in tenants.iterrows():
        bal = calculate_balance(t['id'])
        color = ""
        if bal < 0:
            color = "🔴"
        elif bal == 0:
            # check if current month paid
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT SUM(amount) FROM transactions WHERE tenant_id=? AND strftime('%Y-%m', date)=?",
                      (t['id'], datetime.date.today().strftime("%Y-%m")))
            paid = c.fetchone()[0] or 0
            if paid < t['monthly_rent']:
                color = "🟡"
            conn.close()
        rows.append([t['name'], t['monthly_rent'], bal, color, t['rental_address'], t['original_address']])
    df = pd.DataFrame(rows, columns=["Tenant", "Monthly Rent", "Balance", "Status", "Rental Address", "Original Address"])
    st.dataframe(df)

def page_ledger():
    st.title("📒 Tenant Ledger")

    tenants = get_tenants()
    tenant_names = {row['name']: row['id'] for _, row in tenants.iterrows()}
    tenant_name = st.selectbox("Select Tenant", list(tenant_names.keys()))
    tid = tenant_names[tenant_name]

    transactions = get_transactions()
    ledger = transactions[transactions['tenant_id'] == tid]

    st.write(f"### Ledger for {tenant_name}")
    st.dataframe(ledger)

    balance = calculate_balance(tid)
    if balance < 0:
        st.error(f"Outstanding Balance: {balance}")
    elif balance == 0:
        st.warning("Current month not paid fully")
    else:
        st.success(f"Advance Balance: {balance}")

def page_backup_restore():
    st.title("💾 Backup & Restore")

    with open(DB_FILE, "rb") as f:
        st.download_button("Download Backup", f, file_name="rent_backup.db")

    uploaded = st.file_uploader("Upload Backup (rent.db)", type=["db"])
    if uploaded:
        with open(DB_FILE, "wb") as f:
            f.write(uploaded.read())
        st.success("Database restored! Please refresh the app.")

# -----------------------------
# Main App
# -----------------------------
def main():
    st.set_page_config(page_title="Rent Collection System", layout="wide")
    init_db()

    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if not st.session_state.logged_in:
        page_login()
        return

    st.sidebar.title(f"Welcome {st.session_state.username} ({st.session_state.role})")
    choice = None

    if st.session_state.role == "admin":
        choice = st.sidebar.radio("Navigate", 
            ["Tenant Management", "Rent Collection", "Reports", "Ledger", "Backup & Restore"])
    else:  # employee
        choice = st.sidebar.radio("Navigate", ["Rent Collection"])

    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.username = None
        st.session_state.role = None
        st.experimental_rerun()

    if choice == "Tenant Management":
        page_tenant_management()
    elif choice == "Rent Collection":
        page_rent_collection()
    elif choice == "Reports":
        page_reports()
    elif choice == "Ledger":
        page_ledger()
    elif choice == "Backup & Restore":
        page_backup_restore()

if __name__ == "__main__":
    main()
