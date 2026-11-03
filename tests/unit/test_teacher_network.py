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


def test_local_teacher_code_is_masked_and_can_be_revealed():
    run_teacher_script([
        {"status": 200, "body": {
            "code": "4831", "teacher_code": "064831", "join_url": "http://lan.test/?code=4831",
        }},
        {"status": 200, "body": {"code": "4831", "join_url": "http://lan.test/?code=4831"}},
    ], r'''
  await sandbox.init();
  const panel = elements.get("local-teacher-code-panel");
  const display = elements.get("local-teacher-code");
  const toggle = elements.get("toggle-local-teacher-code");
  assert.equal(panel.hidden, false);
  assert.equal(display.textContent, "••••••");
  toggle.dispatchEvent(new Event("click"));
  assert.equal(display.textContent, "064831");
  toggle.dispatchEvent(new Event("click"));
  assert.equal(display.textContent, "••••••");
  toggle.dispatchEvent(new Event("click"));
  await sandbox.refreshJoinInfo();
  assert.equal(panel.hidden, true);
  assert.equal(display.textContent, "••••••");
''')


def test_teacher_reconnects_with_the_accepted_code_after_join_info_changes():
    run_teacher_script([
        {"status": 200, "body": {
            "code": "4831", "teacher_code": "064831", "join_url": "http://lan.test/?code=4831",
        }},
        {"status": 200, "body": {
            "code": "4832", "teacher_code": "106483", "join_url": "http://lan.test/?code=4832",
        }},
    ], r'''
  await sandbox.init();
  const first = sockets[0];
  first.dispatchEvent(new Event("open"));
  first.emit({type: "joined", session_state: "idle"});
  await timers.shift().fn();
  assert.equal(elements.get("join-code").textContent, "4832");
  assert.equal(elements.get("join-code-live").textContent, "4832");
  assert.equal(elements.get("join-url").textContent, "http://lan.test/?code=4832");
  assert.equal(elements.get("teacher-code").value, "");
  first.close();
  timers.find(timer => timer.delay === 2000).fn();
  const second = sockets[1];
  second.dispatchEvent(new Event("open"));
  assert.deepEqual(sent, Array(2).fill({type: "join", role: "teacher", code: "064831"}));
  second.emit({type: "join_rejected", reason: "bad_code"});
  assert.equal(elements.get("teacher-join-form").hidden, false);
  assert.match(elements.get("teacher-join-error").textContent, /先生コードが違います/);
  assert.equal(elements.get("start-btn").disabled, true);
  assert.equal(timers.filter(timer => timer.delay === 2000).length, 1);
''')
