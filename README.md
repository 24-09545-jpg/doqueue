# DSA System (E-Sign Portal)

Flask app for routing organization event letters through the DSA signing hierarchy.

## Running

```
pip install -r requirements.txt
python app.py
```

On first start the app creates `dsa.sqlite3` and a default moderator account, printed in the console
(`moderator@g.batstate-u.edu.ph` / `ChangeMe123!` unless `DSA_MODERATOR_EMAIL` / `DSA_MODERATOR_PASSWORD` are set).
**Change that password** (Moderator Portal → Accounts → Edit).

The official signing route (Organization Adviser → Student Coordinator → Office of Student Organization →
Program Chairperson → Department Chairperson → College Dean → Vice Chancellor for Academic Affairs →
Vice Chancellor for Administration and Finance) is set up automatically on first start.

Optional demo data (a signatory account for each of the 8 offices and an organization account; password `demo1234`):

```
python seed_demo.py
```

Environment variables: `DSA_SECRET_KEY` (set this in production), `DSA_DB_PATH`, `DSA_UPLOAD_FOLDER`.

## Portals

| Portal | URL | Who | Main pages |
|---|---|---|---|
| User | `/login` | Organizations | Dashboard (progress tracker, remarks, resubmit, download), Request Signature, Sign Yourself |
| Admin | `/admin/login` | Signatory offices | Queue, Signing History |
| Moderator | `/moderator/login` | DSA management team | Overview, Letters, Offices, Accounts, Signing Hierarchy, Website Content, Activity Log |

Moderator setup order: **Offices** → **Accounts** (create each office's signatory account) → **Signing Hierarchy**.
Organizations can't submit letters until a hierarchy exists.

## Workflow rules (enforced on the server)

- A letter goes to the offices in hierarchy order. An office can only open or download a letter once
  the letter has reached it in the current routing round. Before that, direct URLs return 404.
- Office accounts: if an office has a *designated signatory*, only that account can apply the signature.
  Other staff accounts can review, add remarks, **Approve** (status becomes *Waiting for Signature*), return or disapprove.
- **Return for Revision** and **Disapprove** both need a reason. They stop the route and reset progress to 0%.
  The organization can then edit the letter and/or upload a new PDF and resubmit, and the route starts again from the first office.
  Earlier versions, remarks and step history are kept.
- Letters past their expiry date are moved to *Expired*. They can be resubmitted with a new date.
- Hierarchy and office changes apply only to new submissions and resubmissions. An office that still has letters in its queue can't be deactivated.
- When the last office signs, the next number from one central counter is assigned (`DSA-<year>-0001`, `-0002`, …).
  The number is issued inside the same database transaction as the signature, so numbers are never duplicated.
  It is stamped at the bottom-left of every page of the final PDF and never changes.

## Signatures

Settings → upload a PNG (JPG is also accepted). By default a white background is made transparent and the image is trimmed.
When signing, you can drag the signature to move it and drag a corner to resize it (proportions are kept).
The preview uses pdf.js, so the position you see is the position in the final PDF.

## Tests

```
python -m pytest tests
```

`tests/test_workflow.py` runs the whole flow through the real routes: setup, submission, sequential access,
endorse/sign, return, resubmit, final signature, tracking numbers, and access control.
