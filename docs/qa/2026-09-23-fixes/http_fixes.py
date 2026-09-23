"""Real HTTP preview/confirm audit. Run against disposable QA port 8012 only."""
import importlib.util
import json
import re
import uuid
from pathlib import Path
OUT=Path(__file__).resolve().parent
OLD=OUT.parent/"2026-09-23"
spec=importlib.util.spec_from_file_location("old_audit",OLD/"http/import_http_audit.py")
a=importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
a.BASE="http://127.0.0.1:8012"
a.OUT=OUT
a.FIXTURES=OLD/"fixtures"
a.report={"code_base":"1948d7b","branch":"codex/qa-fixes-2026-09-23","base_url":a.BASE,"method":"Real HTTP, cookiejar, CSRF. No live AI.","checks":{}}
status,page,_=a.call("/login/")
token=re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"',page).group(1)
status,page,_=a.call("/login/",{"username":"hr","password":"career-demo-2026","csrfmiddlewaretoken":token})
a.check("hr_login",status==200 and "Профили сотрудников" in page)
before=a.snapshot()
boundary="Stage2"+uuid.uuid4().hex
body=b""
for field,name in [("employees","employees.json"),("history","activity_history.csv")]:
    body+=("--"+boundary+'\r\nContent-Disposition: form-data; name="'+field+'"; filename="'+name+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    body+=(a.FIXTURES/name).read_bytes()+b"\r\n"
body+=("--"+boundary+"--\r\n").encode()
status,page,ms=a.call("/hr/import/",raw=body,content_type="multipart/form-data; boundary="+boundary)
draft=re.search(r'name="draft" value="([^"]+)"',page).group(1)
(OUT/"import-preview.txt").write_text(a.document_text(page),encoding="utf-8")
a.check("preview_does_not_create_profiles",a.snapshot()==before and all(x["api_status"]==404 for x in before.values()),status=status,elapsed_ms=ms)
status,page,ms=a.call("/hr/import/",{"action":"confirm","draft":draft})
(OUT/"import-confirmed.txt").write_text(a.document_text(page),encoding="utf-8")
a.check("confirmed_import",status==200 and "Загрузка завершена" in page,status=status,elapsed_ms=ms)
after=a.snapshot()
a.report["profiles"]=after
a.check("three_profiles_history_coverage", [after[k]["history_count"] for k in sorted(after)]==[4,1,1] and [after[k]["api"]["coverage"] for k in sorted(after)]==[92,100,97])
a.call("/hr/import/",{"action":"confirm","draft":draft})
a.check("repeat_confirm_no_profile_history_change",a.snapshot()==after)
for ident in ["QA001","QA002","QA003"]:
    status,body,ms=a.call("/api/people/"+ident+"/recommendations/",{},as_json=True)
    a.report.setdefault("recommendations",{})[ident]={"status":status,"elapsed_ms":ms,"body":json.loads(body)}
a.save()
