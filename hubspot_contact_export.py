#!/usr/bin/env python3
from pathlib import Path
import argparse,csv,sqlite3,html,sys
from datetime import datetime

CALL_HEADERS=["Email <CONTACT email>","Call notes <CALL hs_call_body>","Activity date <CALL hs_timestamp>"]
EMAIL_HEADERS=["Email <CONTACT email>","Email Direction <EMAIL hs_email_direction>","Email body <EMAIL hs_email_html>","Activity date <EMAIL hs_timestamp>"]
NOTE_HEADERS=["Email <CONTACT email>","Note body <NOTE hs_note_body>","Activity date <NOTE hs_timestamp>"]

def ts(v):
    if isinstance(v,(int,float)): return int(v if v>100000000000 else v*1000)
    return int(datetime.fromisoformat(str(v).replace("Z","+00:00")).timestamp()*1000)

p=argparse.ArgumentParser()
p.add_argument("database",type=Path)
p.add_argument("--contact-id",type=int,required=True)
p.add_argument("--organisation-id",type=int,default=1)
p.add_argument("--output",type=Path,default=Path("output"))
a=p.parse_args()

a.output.mkdir(exist_ok=True)
conn=sqlite3.connect(a.database)
conn.row_factory=sqlite3.Row

contact=conn.execute("SELECT email FROM JobAdderContacts WHERE JobAdderOrganisationId=? AND contactId=?",(a.organisation_id,a.contact_id)).fetchone()
if not contact: raise SystemExit("Contact not found")
email=contact["email"]

cw=csv.DictWriter(open(a.output/"calls.csv","w",newline="",encoding="utf-8-sig"),fieldnames=CALL_HEADERS);cw.writeheader()
ew=csv.DictWriter(open(a.output/"emails.csv","w",newline="",encoding="utf-8-sig"),fieldnames=EMAIL_HEADERS);ew.writeheader()
nw=csv.DictWriter(open(a.output/"notes.csv","w",newline="",encoding="utf-8-sig"),fieldnames=NOTE_HEADERS);nw.writeheader()

rows=conn.execute("""SELECT n.* FROM JobAdderNoteContacts nc
JOIN JobAdderNotes n ON n.JobAdderOrganisationId=nc.JobAdderOrganisationId AND n.noteId=nc.noteId
WHERE nc.JobAdderOrganisationId=? AND nc.contactId=? ORDER BY n.createdAt""",(a.organisation_id,a.contact_id))
for r in rows:
    body=html.escape(r["text"] or "").replace("\n","<br>")
    t=(r["type"] or "").upper()
    if "CALL" in t:
        cw.writerow({CALL_HEADERS[0]:email,CALL_HEADERS[1]:body,CALL_HEADERS[2]:str(ts(r["createdAt"]))})
    elif "EMAIL" in t:
        ew.writerow({EMAIL_HEADERS[0]:email,EMAIL_HEADERS[1]:"EMAIL",EMAIL_HEADERS[2]:body,EMAIL_HEADERS[3]:str(ts(r["createdAt"]))})
    else:
        nw.writerow({NOTE_HEADERS[0]:email,NOTE_HEADERS[1]:body,NOTE_HEADERS[2]:str(ts(r["createdAt"]))})
print("Done")
