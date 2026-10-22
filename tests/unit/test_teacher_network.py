"""先生画面のAPI応答→QR表示→接続先消失時の消去をDOM境界で確認する。"""

from tests.teacher_page import run_teacher_script


def test_teacher_clears_qr_and_url_when_network_becomes_invalid():
    run_teacher_script([
        {"status": 200, "body": {"code": "4831", "join_url": "http://10.53.64.130:8000/?code=4831"}},
        {"status": 503, "body": {"detail": "接続先が消失しました。再起動してください。"}},
    ], r'''
  elements.get("page-error").textContent = "別の先生タブに接続を引き継ぎました。";
  elements.get("page-error").hidden = false;
  await sandbox.refreshJoinInfo();
  assert.equal(elements.get("page-error").hidden, false);
  assert.equal(elements.get("qr").textContent, "http://10.53.64.130:8000/?code=4831");
  assert.equal(elements.get("join-url").textContent, elements.get("qr").textContent);
  await sandbox.refreshJoinInfo();
  for (const id of ["qr", "join-url", "join-code"]) assert.equal(elements.get(id).textContent, "");
  assert.equal(elements.get("network-error").hidden, false);
  assert.match(elements.get("network-error").textContent, /再起動/);
''')
