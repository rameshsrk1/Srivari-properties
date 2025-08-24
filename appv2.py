import streamlit as st import sqlite3 import hashlib import datetime import pandas as pd from io import BytesIO

============================

Database Setup

============================

def get_connection(): return sqlite3.connect("rent_collection.db", check_same_thread=False)

def init_db(): conn = get_connection() c = conn.cursor()

# Users table
c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
""")

# Tenants table
c.execute("""
    CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        phone TEXT,
        rent INTEGER,
        property_address TEXT,
        original_address TEXT,
        opening_balance INTEGER DEFAULT 0,
        advance_amount INTEGER DEFAULT 0,
        start_date TEXT,
        vacated INTEGER DEFAULT 0
    )
""")

# Transactions table
c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER,
        date TEXT,
        amount INTEGER,
        remarks TEXT,
        FOREIGN KEY(tenant_id) REFERENCES tenants(id)
    )
""")

conn.commit()
conn.close()

============================

Utility Functions

============================

def hash_password(password): return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hashed): return hash_password(password) == hashed

def add_user(username, password, role): conn = get_connection() c = conn.cursor() try: c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, hash_password(password), role)) conn.commit() except: pass conn.close()

============================

Login Page

============================

def login(): st.title("🔑 Login") username = st.text_input("Username") password = st.text_input("Password", type="password") if st.button("Login"): conn = get_connection() c = conn.cursor() c.execute("SELECT password, role FROM users WHERE username=?", (username,)) row = c.fetchone() conn.close() if row and verify_password(password, row[0]): st.session_state["logged_in"] = True st.session_state["username"] = username st.session_state["role"] = row[1] st.experimental_rerun() else: st.error("Invalid credentials")

============================

Tenant Management

============================

def tenant_management(): st.header("🏢 Tenant Management")

with st.form("add_tenant_form"):
    name = st.text_input("Tenant Name")
    phone = st.text_input("Phone")
    rent = st.number_input("Monthly Rent", step=100, value=0)
    property_address = st.text_input("Rental Property Address")
    original_address = st.text_input("Original Address")
    opening_balance = st.number_input("Opening Balance", step=100, value=0)
    advance_amount = st.number_input("Advance Amount (Security Deposit)", step=100, value=0)
    start_date = st.date_input("Start Date", datetime.date.today())
    submit = st.form_submit_button("Add Tenant")

    if submit:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""
            INSERT INTO tenants (name, phone, rent, property_address, original_address, opening_balance, advance_amount, start_date, vacated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (name, phone, rent, property_address, original_address,
              opening_balance + advance_amount, advance_amount, start_date))
        conn.commit()
        conn.close()
        st.success(f"Tenant {name} added successfully!")

# Show tenants
conn = get_connection()
df = pd.read_sql("SELECT * FROM tenants WHERE vacated=0", conn)
conn.close()
st.subheader("Active Tenants")
st.dataframe(df)

============================

Rent Collection

============================

def rent_collection(): st.header("💰 Rent Collection")

conn = get_connection()
tenants = pd.read_sql("SELECT id, name FROM tenants WHERE vacated=0", conn)

tenant_id = st.selectbox("Select Tenant", tenants["id"], format_func=lambda x: tenants.loc[tenants["id"] == x, "name"].values[0])
amount = st.number_input("Amount Received", step=100, value=0)
remarks = st.text_area("Remarks")
if st.button("Record Payment"):
    c = conn.cursor()
    c.execute("INSERT INTO transactions (tenant_id, date, amount, remarks) VALUES (?, ?, ?, ?)",
              (tenant_id, str(datetime.date.today()), amount, remarks))
    conn.commit()
    st.success("Payment recorded!")

conn.close()

============================

Ledger View

============================

def ledger_view(): st.header("📒 Tenant Ledger") conn = get_connection() tenants = pd.read_sql("SELECT id, name FROM tenants WHERE vacated=0", conn)

tenant_id = st.selectbox("Select Tenant", tenants["id"], format_func=lambda x: tenants.loc[tenants["id"] == x, "name"].values[0])
df = pd.read_sql("SELECT date, amount, remarks FROM transactions WHERE tenant_id=?", conn, params=(tenant_id,))
st.subheader("Transaction History")
st.dataframe(df)
conn.close()

============================

Reports

============================

def reports(): st.header("📊 Reports") conn = get_connection()

tenants = pd.read_sql("SELECT * FROM tenants WHERE vacated=0", conn)
balances = []

for _, tenant in tenants.iterrows():
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE tenant_id=?", (tenant["id"],))
    total_paid = c.fetchone()[0] or 0

    start_date = datetime.datetime.strptime(tenant["start_date"], "%Y-%m-%d").date()
    months_stayed = (datetime.date.today().year - start_date.year) * 12 + (datetime.date.today().month - start_date.month)

    balance = (months_stayed * tenant["rent"]) - total_paid + tenant["opening_balance"]

    balances.append([tenant["name"], tenant["phone"], tenant["rent"], balance])

df = pd.DataFrame(balances, columns=["Name", "Phone", "Rent", "Balance"])

def highlight(row):
    if row["Balance"] < 0:
        return ["background-color: red"] * len(row)
    elif row["Balance"] > 0:
        return ["background-color: yellow"] * len(row)
    else:
        return [""] * len(row)

st.dataframe(df.style.apply(highlight, axis=1))

# Download as Excel
if st.button("📥 Download Report"):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    st.download_button("Download Excel", data=output.getvalue(), file_name="report.xlsx")

conn.close()

============================

Backup & Restore

============================

def backup_restore(): st.header("💾 Backup & Restore") conn = get_connection() conn.close()

with open("rent_collection.db", "rb") as f:
    st.download_button("Download Database", f, file_name="backup_rent_collection.db")

uploaded = st.file_uploader("Upload Backup", type="db")
if uploaded:
    with open("rent_collection.db", "wb") as f:
        f.write(uploaded.getbuffer())
    st.success("Database restored successfully!")

============================

Main App

============================

def main(): init_db()

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

if not st.session_state["logged_in"]:
    login()
else:
    st.sidebar.title("Navigation")
    role = st.session_state["role"]

    if role == "admin":
        page = st.sidebar.radio("Go to", ["Tenant Management", "Rent Collection", "Ledger", "Reports", "Backup & Restore"])
    else:  # employee
        page = st.sidebar.radio("Go to", ["Rent Collection", "Reports"])

    if page == "Tenant Management":
        tenant_management()
    elif page == "Rent Collection":
        rent_collection()
    elif page == "Ledger":
        ledger_view()
    elif page == "Reports":
        reports()
    elif page == "Backup & Restore":
        backup_restore()

if name == "main": main()

