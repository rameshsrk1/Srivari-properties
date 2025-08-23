# app.py
import streamlit as st
import sqlite3
import pandas as pd
import datetime as dt
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import hashlib
import os

DB_FILE = "rent_collection.db"

# ------------- DB Helpers -------------
def conn():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

def init_db():
    c = conn()
    cur = c.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','employee'))
    )
    """)

    # tenants
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tenants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        rent REAL NOT NULL,
        rental_address TEXT,
        original_address TEXT,
        joining_date DATE NOT NULL,
        opening_balance REAL DEFAULT 0
    )
    """)

    # payments (money received)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        payment_date DATE NOT NULL,
        amount REAL NOT NULL,
        mode TEXT,
        employee TEXT,
        remarks TEXT,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )
    """)

    # charges (rent/fees added each month)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS charges(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL,
        charge_date DATE NOT NULL,
        amount REAL NOT NULL,
        note TEXT,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )
    """)

    # default users if none exist
    cur.execute("SELECT COUNT(*) FROM users")
    if (cur.fetchone() or [0])[0] == 0:
        cur.execute("INSERT INTO users (username,password_hash,role) VALUES (?,?,?)",
                    ("admin", hash_pw("admin123"), "admin"))
        cur.execute("INSERT INTO users (username,password_hash,role) VALUES (?,?,?)",
                    ("employee", hash_pw("emp123"), "employee"))
    c.commit(); c.close()

def auth_user(username, password):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT id, username, password_hash, role FROM users WHERE username=?", (username,))
    row = cur.fetchone()
    c.close()
    if row and row[2] == hash_pw(password):
        return {"id": row[0], "username": row[1], "role": row[3]}
    return None

# ------------- Ledger math -------------
# We define NET BALANCE from the user's requirement (red for negative):
# net = payments_total - (opening_balance + charges_total)
#   < 0  => owes (arrears)  -> 🔴 red
#   >=0  => on time/advance
def tenant_net_balance(tenant_id: int):
    c = conn(); cur = c.cursor()
    cur.execute("SELECT opening_balance FROM tenants WHERE id=?", (tenant_id,))
    ob = (cur.fetchone() or [0])[0] or 0

    cur.execute("SELECT COALESCE(SUM(amount),0) FROM charges WHERE tenant_id=?", (tenant_id,))
    charges = (cur.fetchone() or [0])[0] or 0

    cur.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE tenant_id=?", (tenant_id,))
    payments = (cur.fetchone() or [0])[0] or 0
    c.close()
    return payments - (ob + charges)

def month_has_delay(tenant_id: int, year_month: str):
    """Yellow highlight if current-month paid < current-month charges."""
    c = conn(); cur = c.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) FROM charges
        WHERE tenant_id=? AND strftime('%Y-%m', charge_date)=?
    """, (tenant_id, year_month))
    cm_charges = (cur.fetchone() or [0])[0] or 0

    cur.execute("""
        SELECT COALESCE(SUM(amount),0) FROM payments
        WHERE tenant_id=? AND strftime('%Y-%m', payment_date)=?
    """, (tenant_id, year_month))
    cm_pay = (cur.fetchone() or [0])[0] or 0
    c.close()
    return cm_pay < cm_charges

def ensure_backfilled_charges_for_tenant(tenant_id: int):
    """
    Ensure monthly rent charges exist from joining_date through current month.
    If rent changes later, future months will use the updated rent automatically.
    """
    today = dt.date.today()
    first_this_month = today.replace(day=1)
    y_m_now = first_this_month.strftime("%Y-%m")

    c = conn(); cur = c.cursor()
    cur.execute("SELECT joining_date, rent FROM tenants WHERE id=?", (tenant_id,))
    row = cur.fetchone()
    if not row:
        c.close(); return
    join_date = dt.date.fromisoformat(row[0])
    rent_now = row[1]

    # find last month charged
    cur.execute("""
        SELECT MAX(strftime('%Y-%m', charge_date))
        FROM charges WHERE tenant_id=?
    """, (tenant_id,))
    last_ym = (cur.fetchone() or [None])[0]

    # start month is max(join_month, last_ym+1)
    start_date = join_date.replace(day=1)
    if last_ym:
        y, m = map(int, last_ym.split("-"))
        last_first = dt.date(y, m, 1)
        # next month
        nm_y = last_first.year + (1 if last_first.month == 12 else 0)
        nm_m = 1 if last_first.month == 12 else last_first.month + 1
        next_first = dt.date(nm_y, nm_m, 1)
        if next_first > start_date:
            start_date = next_first

    # loop months to current inclusive
    cur2 = c.cursor()
    cursor_date = start_date
    while cursor_date <= first_this_month:
        ym = cursor_date.strftime("%Y-%m")
        # fetch rent in that month (rent may have changed; we’ll fetch current rent each insert time.
        cur2.execute("SELECT rent FROM tenants WHERE id=?", (tenant_id,))
        rent_for_month = (cur2.fetchone() or [rent_now])[0] or rent_now
        cur.execute("""
            INSERT INTO charges (tenant_id, charge_date, amount, note)
            VALUES (?, ?, ?, ?)
        """, (tenant_id, cursor_date.isoformat(), float(rent_for_month), "Monthly Rent"))
        # advance one month
        ny = cursor_date.year + (1 if cursor_date.month == 12 else 0)
        nm = 1 if cursor_date.month == 12 else cursor_date.month + 1
        cursor_date = dt.date(ny, nm, 1)

    c.commit(); c.close()

def ensure_backfilled_charges_for_all():
    c = conn(); df = pd.read_sql_query("SELECT id FROM tenants", c); c.close()
    for tid in df["id"].tolist():
        ensure_backfilled_charges_for_tenant(tid)

# ------------- PDF Receipt -------------
def build_receipt_pdf(tenant_name, flat_addr, payment_date, amount, mode, remarks, new_net_balance):
    bio = BytesIO()
    pdf = canvas.Canvas(bio, pagesize=A4)
    pdf.setTitle("Rent Receipt")

    x, y = 50, 800
    pdf.setFont("Helvetica-Bold", 16); pdf.drawString(x, y, "RENT RECEIPT")
    pdf.setFont("Helvetica", 11)
    y -= 30; pdf.drawString(x, y, f"Tenant: {tenant_name}")
    y -= 20; pdf.drawString(x, y, f"Rental Address: {flat_addr or '-'}")
    y -= 20; pdf.drawString(x, y, f"Date: {payment_date}")
    y -= 20; pdf.drawString(x, y, f"Payment Mode: {mode or '-'}")
    y -= 20; pdf.drawString(x, y, f"Remarks: {remarks or '-'}")
    y -= 30; pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, f"Amount Received: ₹{amount:,.2f}")
    y -= 20
    bal_label = "Net Balance (payments - (opening + charges))"
    pdf.setFont("Helvetica", 10); pdf.drawString(x, y, bal_label)
    y -= 18; pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(x, y, f"After This Payment: ₹{new_net_balance:,.2f}")
    # footer
    pdf.showPage(); pdf.save()
    bio.seek(0)
    return bio

# ------------- Pages -------------
def page_login():
    st.title("🏠 Rent Collection — Login")

    u = st.text_input("Username")
    p = st.text_input("Password", type="password")
    if st.button("Login"):
        user = auth_user(u, p)
        if user:
            st.session_state["user"] = user
            st.success(f"Welcome {user['username']} ({user['role']})")
            st.rerun()
        else:
            st.error("Invalid username or password")

def page_tenants():
    st.title("👥 Tenant Management (Admin)")

    with st.expander("➕ Add Tenant", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("Tenant Name")
            rent = st.number_input("Monthly Rent", min_value=0.0, step=100.0)
            joining_date = st.date_input("Joining Date", value=dt.date.today())
            opening_balance = st.number_input("Opening Balance (from notebook, if any)", value=0.0, step=100.0)
        with col2:
            rental_addr = st.text_area("Rental Property Address")
            original_addr = st.text_area("Original Address")

        if st.button("Add Tenant"):
            if not name or rent is None:
                st.error("Please fill Name and Monthly Rent")
            else:
                c = conn(); cur = c.cursor()
                cur.execute("""
                    INSERT INTO tenants(name, rent, rental_address, original_address, joining_date, opening_balance)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (name, rent, rental_addr, original_addr, joining_date.isoformat(), opening_balance))
                t_id = cur.lastrowid
                c.commit(); c.close()

                # Backfill charges starting from joining date up to current month
                ensure_backfilled_charges_for_tenant(t_id)
                st.success("Tenant added and charges backfilled ✔")

    # List / Edit / Delete / Export
    c = conn()
    df = pd.read_sql_query("SELECT * FROM tenants", c)
    c.close()
    st.subheader("Tenant List")
    st.dataframe(df, use_container_width=True)

    with st.expander("✏️ Edit / ❌ Delete Tenant"):
        if df.empty:
            st.info("No tenants yet.")
        else:
            tid = st.selectbox("Select Tenant", df["id"], format_func=lambda x: df.loc[df["id"]==x,"name"].iloc[0])
            row = df[df["id"]==tid].iloc[0]
            e1, e2 = st.columns(2)
            with e1:
                new_name = st.text_input("Name", row["name"])
                new_rent = st.number_input("Monthly Rent", value=float(row["rent"]), step=100.0)
                new_join = st.date_input("Joining Date", value=dt.date.fromisoformat(row["joining_date"]))
            with e2:
                new_rental = st.text_area("Rental Address", row["rental_address"] or "")
                new_orig = st.text_area("Original Address", row["original_address"] or "")
                new_ob = st.number_input("Opening Balance", value=float(row["opening_balance"]), step=100.0)

            ucol1, ucol2, ucol3 = st.columns([1,1,2])
            with ucol1:
                if st.button("Update"):
                    c = conn(); cur = c.cursor()
                    cur.execute("""
                        UPDATE tenants SET name=?, rent=?, rental_address=?, original_address=?,
                        joining_date=?, opening_balance=? WHERE id=?
                    """, (new_name, new_rent, new_rental, new_orig, new_join.isoformat(), new_ob, int(tid)))
                    c.commit(); c.close()
                    st.success("Tenant updated.")
                    st.rerun()
            with ucol2:
                if st.button("Delete", type="primary"):
                    c = conn(); cur = c.cursor()
                    # delete child rows first
                    cur.execute("DELETE FROM payments WHERE tenant_id=?", (int(tid),))
                    cur.execute("DELETE FROM charges  WHERE tenant_id=?", (int(tid),))
                    cur.execute("DELETE FROM tenants WHERE id=?", (int(tid),))
                    c.commit(); c.close()
                    st.success("Tenant and related records deleted.")
                    st.rerun()

    # Export
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download Tenants (CSV)", csv, file_name="tenants.csv", mime="text/csv")

def page_collect_rent():
    st.title("💰 Collect Rent (Employee/Admin)")

    # ensure monthly charges are backfilled before collection
    ensure_backfilled_charges_for_all()

    c = conn()
    tenants = pd.read_sql_query("SELECT id, name, rental_address FROM tenants ORDER BY name", c)
    c.close()
    if tenants.empty:
        st.warning("No tenants available. Ask admin to add tenants.")
        return

    tname = st.selectbox("Tenant", tenants["name"])
    trow = tenants[tenants["name"]==tname].iloc[0]
    tid = int(trow["id"])

    # Show current month status & net balance
    today = dt.date.today()
    ym = today.strftime("%Y-%m")
    net_before = tenant_net_balance(tid)

    st.info(f"Net Balance before payment (payments - (opening + charges)): **₹{net_before:,.2f}**")

    # current month status
    delayed = month_has_delay(tid, ym)
    if delayed:
        st.warning("This month's rent is not fully paid (🟡).")

    amt = st.number_input("Amount Received (₹)", min_value=0.0, step=100.0)
    mode = st.selectbox("Mode", ["Cash","UPI","Bank Transfer","Cheque","Other"])
    remarks = st.text_area("Remarks")
    pdate = st.date_input("Payment Date", value=today)

    if st.button("Save Payment"):
        c = conn(); cur = c.cursor()
        employee_name = st.session_state.get("user", {}).get("username", "")
        cur.execute("""
            INSERT INTO payments(tenant_id, payment_date, amount, mode, employee, remarks)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (tid, pdate.isoformat(), float(amt), mode, employee_name, remarks))
        c.commit(); c.close()
        st.success("Payment saved ✔")

        # new net balance and receipt
        net_after = tenant_net_balance(tid)
        pdf = build_receipt_pdf(
            tenant_name=tname,
            flat_addr=trow["rental_address"],
            payment_date=pdate.isoformat(),
            amount=float(amt),
            mode=mode,
            remarks=remarks,
            new_net_balance=net_after
        )
        st.download_button("⬇️ Download Receipt (PDF)", data=pdf, file_name=f"Receipt_{tname}_{pdate.isoformat()}.pdf", mime="application/pdf")
        st.info(f"Net Balance after payment: **₹{net_after:,.2f}**")

def page_reports():
    st.title("📊 Reports (Employee/Admin)")

    # Always backfill before reporting to keep things consistent
    ensure_backfilled_charges_for_all()

    c = conn()
    tenants = pd.read_sql_query("""
        SELECT id, name, rent, rental_address, original_address, joining_date, opening_balance
        FROM tenants ORDER BY name
    """, c)
    c.close()
    if tenants.empty:
        st.info("No tenants yet.")
        return

    # compute net balance per tenant
    nets = []
    cm_delay = []
    ym = dt.date.today().strftime("%Y-%m")
    for _, r in tenants.iterrows():
        nb = tenant_net_balance(int(r["id"]))
        nets.append(nb)
        cm_delay.append(month_has_delay(int(r["id"]), ym))
    tenants["Net Balance"] = nets
    tenants["This Month Delayed?"] = cm_delay

    # style: red for negative net, yellow if delayed this month
    def row_style(row):
        styles = []
        for col in tenants.columns:
            style = ""
            if col == "Net Balance" and row["Net Balance"] < 0:
                style = "background-color: red; color: white;"
            elif col == "This Month Delayed?" and row["This Month Delayed?"]:
                style = "background-color: yellow; color: black;"
            styles.append(style)
        return styles

    st.dataframe(tenants.style.apply(row_style, axis=1), use_container_width=True)

    # quick filters / export
    st.subheader("Export")
    csv = tenants.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download Report (CSV)", csv, file_name="report.csv", mime="text/csv")

    # collections view
    st.subheader("All Collections")
    c = conn()
    coll = pd.read_sql_query("""
        SELECT p.id, t.name as tenant, p.payment_date, p.amount, p.mode, p.employee, p.remarks
        FROM payments p JOIN tenants t ON t.id=p.tenant_id
        ORDER BY p.payment_date DESC, p.id DESC
    """, c)
    c.close()
    st.dataframe(coll, use_container_width=True)

def page_backup_restore():
    st.title("🗄️ Backup & Restore (Admin)")

    # Download db
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "rb") as f:
            st.download_button("⬇️ Download SQLite DB", f, file_name="rent_backup.db", mime="application/octet-stream")
    else:
        st.warning("No database file found.")

    # Upload db
    up = st.file_uploader("Upload a SQLite backup (.db)", type=["db"])
    if up is not None:
        with open(DB_FILE, "wb") as f:
            f.write(up.getbuffer())
        st.success("Database restored. Please reload the app.")
        st.rerun()
def page_ledger():
    import itertools
    st.title("📒 Tenant Ledger (Employee/Admin)")

    # Backfill monthly charges to ensure ledger completeness
    ensure_backfilled_charges_for_all()

    # ---- Pick Tenant ----
    c = conn()
    tenants = pd.read_sql_query(
        "SELECT id, name, rental_address, joining_date, opening_balance FROM tenants ORDER BY name", c
    )
    c.close()
    if tenants.empty:
        st.info("No tenants yet.")
        return

    tname = st.selectbox("Tenant", tenants["name"])
    trow = tenants[tenants["name"] == tname].iloc[0]
    tid = int(trow["id"])
    joining_date = dt.date.fromisoformat(trow["joining_date"])
    opening_balance = float(trow["opening_balance"] or 0)

    # ---- Pull charges & payments ----
    c = conn()
    charges = pd.read_sql_query(
        """
        SELECT id, charge_date AS date, amount, COALESCE(note,'') AS note
        FROM charges WHERE tenant_id=? ORDER BY date, id
        """,
        c, params=(tid,)
    )
    pays = pd.read_sql_query(
        """
        SELECT id, payment_date AS date, amount, COALESCE(mode,'') AS mode,
               COALESCE(employee,'') AS employee, COALESCE(remarks,'') AS remarks
        FROM payments WHERE tenant_id=? ORDER BY date, id
        """,
        c, params=(tid,)
    )
    c.close()

    # ---- Build unified ledger events ----
    events = []

    # Opening balance as the very first event (acts like a charge)
    events.append({
        "date": joining_date.isoformat(),
        "type": "Opening",
        "description": "Opening Balance",
        "debit": opening_balance,   # money owed
        "credit": 0.0
    })

    # Charges -> Debit
    for _, r in charges.iterrows():
        events.append({
            "date": r["date"],
            "type": "Charge",
            "description": r["note"] or "Monthly Rent",
            "debit": float(r["amount"] or 0),
            "credit": 0.0
        })

    # Payments -> Credit
    for _, r in pays.iterrows():
        desc = f"{r['mode']}".strip()
        if r["employee"]:
            desc += f" by {r['employee']}"
        if r["remarks"]:
            desc += f" — {r['remarks']}"
        events.append({
            "date": r["date"],
            "type": "Payment",
            "description": desc.strip() or "Payment",
            "debit": 0.0,
            "credit": float(r["amount"] or 0)
        })

    # Sort by date, then by type priority (Opening -> Charge -> Payment) to keep stable ordering on same date
    type_order = {"Opening": 0, "Charge": 1, "Payment": 2}
    events.sort(key=lambda e: (e["date"], type_order.get(e["type"], 9)))

    # ---- Compute running net (payments - (opening + charges)) ----
    running = []
    net = 0.0
    for e in events:
        if e["type"] in ("Opening", "Charge"):
            net -= e["debit"]
        elif e["type"] == "Payment":
            net += e["credit"]
        running.append(net)

    ledger_df = pd.DataFrame(events)
    ledger_df["Running Net"] = running

    # ---- Current month status ----
    ym = dt.date.today().strftime("%Y-%m")
    delayed = month_has_delay(tid, ym)
    net_now = tenant_net_balance(tid)

    st.markdown(
        f"**Net Balance now:** {'🔴' if net_now < 0 else '🟢'} ₹{net_now:,.2f}  "
        f"{'— 🟡 Current month not fully paid' if delayed else ''}"
    )

    # ---- Style: red for negative running balance ----
    def style_row(row):
        styles = []
        for col in ledger_df.columns:
            if col == "Running Net" and row["Running Net"] < 0:
                styles.append("background-color: red; color: white;")
            else:
                styles.append("")
        return styles

    st.dataframe(
        ledger_df[["date", "type", "description", "debit", "credit", "Running Net"]]
        .rename(columns={
            "date": "Date", "type": "Type", "description": "Description",
            "debit": "Debit (₹)", "credit": "Credit (₹)"
        })
        .style.apply(style_row, axis=1),
        use_container_width=True
    )

    # ---- Downloads ----
    csv = ledger_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download Ledger (CSV)", data=csv,
        file_name=f"ledger_{tname.replace(' ', '_')}.csv", mime="text/csv"
    )
# ------------- Main -------------
def main():
    st.set_page_config(page_title="Rent Collection System", page_icon="🏠", layout="wide")
    init_db()

    user = st.session_state.get("user")

    if not user:
        page_login()
        return

    st.sidebar.write(f"**User:** {user['username']} ({user['role']})")
    if user["role"] == "admin":
        page = st.sidebar.radio("Navigate", ["Collect Rent", "Reports", "Tenant Management", "Backup & Restore"])
    else:
        page = st.sidebar.radio("Navigate", ["Collect Rent", "Reports"])

    if st.sidebar.button("Logout"):
        st.session_state.clear()
        st.rerun()

    if page == "Collect Rent":
        page_collect_rent()
    elif page == "Reports":
        page_reports()
    elif page == "Ledger":
        page_ledger()
    elif page == "Tenant Management":
        if user["role"] != "admin":
            st.error("Admin only"); return
        page_tenants()
    elif page == "Backup & Restore":
        if user["role"] != "admin":
            st.error("Admin only"); return
        page_backup_restore()

if __name__ == "__main__":
    main()
