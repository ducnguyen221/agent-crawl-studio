# Mau ket qua `auto` / `direct` (SPR-04 TK1)

Bon tep: `plan-auto.json`, `plan-direct.json`, `result-auto.json`, `result-direct.json`. Chung la ket qua `plan` va `run`
cua ma o commit `f396845` (cuoi SPR-03), sinh **truoc** moi sua ma cua SPR-04, de TK8 va TK9 so lai (SPEC FR21.5, SC29).
`auto` va `direct` giong het nhau vi hai URL deu khong thuoc may chu media; day la hanh vi dung, khong phai loi sinh mau.

## Cach sinh

Mot lenh, Python he thong 3.12 hoac Python cua venv `core` (khong can pytest, khong can `PYTHONPATH`):

    python -X utf8 tests/fixtures/golden/gen_golden.py --work <thu muc rong> --out <thu muc ra>

`gen_golden.py` dung cung may chu fixture loopback va chinh sach tiem (`Policy(ports, ip_ok, resolver)`) nhu
`tests/harness_fixture_run.py`; chinh sach chi la tham so cua ma, khong phai cong tac. Moi che do chay mot lo hai URL:
`/html?size=3000` (trang HTML) va `/file?size=2000` (tep nhi phan, `application/octet-stream`). Voi moi che do: `runner.plan`
roi `runner.run` (ma thoat 0). Sinh hai lan voi hai thu muc `--work` khac nhau cho bon tep giong het tung byte.

## Danh sach khoa chuan hoa (thay bang hang)

| Truong / dang gia tri | Thay bang | Vi sao |
|---|---|---|
| `started_at`, `finished_at` (moi cho) | `<TIMESTAMP>` | thoi gian chay |
| `job_id` va moi chuoi chua gia tri job id | `<JOB_ID>` | dinh danh job sinh ngau nhien |
| `:<cong>` trong chuoi (`url_redacted`, `source_url_redacted`, `final_url_redacted`) | `:<PORT>` | cong cua may chu fixture doi moi lan |
| `url_sha256` (moi muc) | `<URL_SHA256>` | bam cua URL co chua cong |
| duong dan tuyet doi cua thu muc lam viec (`output.root`, `output.parent`, `output.job_dir_template`; ca dang `/` va `\`) | `<WORK>` | thu muc `--work` |

Khong chuan hoa: `artifacts[].sha256`, `bytes`, `path` (ten tep gom 10 ky tu dau cua bam noi dung), `engine_version` (httpx
0.28.1 luc sinh mau), `limits_effective`, `warnings`, `counts`. Neu `gen_golden.py` con thay `Users`, `OpcOS` hoac so cong
trong ket qua thi no dung va khong ghi tep.

## Phep so cua TK8, TK9

So voi cac tep nay sau khi chuan hoa cung kieu, plan/result cua `auto` va `direct` chi duoc khac dung ba cho da khai o SPEC
muc 7: hai khoa moi trong `limits_effective` va `network_bytes` o result. Artifact khong duoc co `role`, `untrusted`,
`chars`, `truncated`.
