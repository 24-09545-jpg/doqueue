"""
Optional: create demo signatory accounts for the 8 official offices and a demo
organization account, so the full workflow can be tried right away.

    python seed_demo.py

Safe to run more than once (existing records are left as they are).
All demo passwords are: demo1234
"""
import db
from db import OFFICIAL_HIERARCHY, execute, now_str, query, transaction
from models import create_user, find_user_by_email

DOMAIN = "@g.batstate-u.edu.ph"
SLUGS = {
    "Organization Adviser": "adviser",
    "Student Coordinator": "coordinator",
    "Office of Student Organization": "oso",
    "Program Chairperson": "program.chair",
    "Department Chairperson": "dept.chair",
    "College Dean": "dean",
    "Vice Chancellor for Academic Affairs": "vcaa",
    "Vice Chancellor for Administration and Finance": "vcaf",
}


def main():
    db.init_db()  # also applies the official signing route on first run
    rows = []
    for order, name in enumerate(OFFICIAL_HIERARCHY, start=1):
        oid = query("SELECT id FROM offices WHERE name = ? COLLATE NOCASE", (name,), one=True)["id"]
        email = f"demo.{SLUGS[name]}{DOMAIN}"
        user = find_user_by_email(email)
        uid = user["id"] if user else create_user(email, "demo1234", f"{name} (Demo)", "office",
                                                  office_id=oid, position="Signatory")
        with transaction():
            execute("UPDATE offices SET signatory_user_id = COALESCE(signatory_user_id, ?) WHERE id = ?", (uid, oid))
        rows.append((order, name, email))

    org_email = f"demo.org{DOMAIN}"
    if not find_user_by_email(org_email):
        with transaction():
            org_id = execute("INSERT INTO organizations(name, contact_email, created_at) VALUES (?, ?, ?)",
                             ("Demo Student Organization", org_email, now_str()))
        create_user(org_email, "demo1234", "Demo Org Officer", "user", organization_id=org_id)

    print("Demo data ready (password for all demo accounts: demo1234)")
    print("  User Portal  /login        ", org_email)
    for order, name, email in rows:
        print(f"  Signatory Portal /admin/login   {order}. {name:<48} {email}")


if __name__ == "__main__":
    main()
