import json

import pytest

from tests.teacher_page import run_teacher_script


def test_joined_save_failure_enables_end_retry_without_live_error():
    run_teacher_script([
        {"status": 200, "body": {
            "code": "4831", "join_url": "http://lan.test/?code=4831",
            "teacher_code": "123456",
        }},
    ], r'''
  sandbox.WebSocket.OPEN = 1;
  await sandbox.init();
  const socket = sockets.at(-1);
  socket.readyState = sandbox.WebSocket.OPEN;
  socket.emit({type: "joined", session_state: "ended", recording: true,
    recording_save_error: "保存できませんでした"});
  const end = elements.get("end-btn");
  assert.equal(elements.get("mic-status").textContent, "サーバーからの警告: 保存できませんでした");
  assert.equal(end.disabled, false);
  end.dispatchEvent(new Event("click"));
  assert.deepEqual(sent, [{type: "control", action: "end"}]);
''')


@pytest.mark.parametrize("other_warning", [False, True])
def test_joined_null_save_failure_clears_only_recording_warning(other_warning):
    run_teacher_script([
        {"status": 200, "body": {
            "code": "4831", "join_url": "http://lan.test/?code=4831",
            "teacher_code": "123456",
        }},
    ], r'''
  sandbox.WebSocket.OPEN = 1;
  await sandbox.init();
  let socket = sockets.at(-1);
  socket.readyState = sandbox.WebSocket.OPEN;
  socket.emit({type: "joined", session_state: "ended", recording: true});
  socket.emit({type: "error", code: "recording_save_failed", message: "保存できませんでした"});
  if (__OTHER_WARNING__) {
    socket.emit({type: "error", code: "recording_pending", message: "保存を待っています"});
  }
  socket.close();
  timers.find(timer => timer.delay === 2000).fn();
  socket = sockets.at(-1);
  socket.readyState = sandbox.WebSocket.OPEN;
  socket.emit({type: "joined", session_state: "ended", recording: true, recording_save_error: null});
  assert.equal(elements.get("end-btn").disabled, true);
  assert.equal(elements.get("mic-status").textContent,
    __OTHER_WARNING__ ? "サーバーからの警告: 保存を待っています" : "");
'''.replace("__OTHER_WARNING__", json.dumps(other_warning)))


@pytest.mark.parametrize("next_state", ["idle", "live", "paused"])
@pytest.mark.parametrize("other_warning", [False, True])
def test_recording_retry_survives_disconnect_until_new_session(next_state, other_warning):
    run_teacher_script([
        {"status": 200, "body": {
            "code": "4831", "join_url": "http://lan.test/?code=4831",
            "teacher_code": "123456",
        }},
    ], r'''
  sandbox.WebSocket.OPEN = 1;
  await sandbox.init();
  let socket = sockets.at(-1);
  socket.readyState = sandbox.WebSocket.OPEN;
  socket.emit({type: "joined", session_state: "ended", recording: true});
  const end = elements.get("end-btn");
  const warning = elements.get("mic-status");
  const saveError = {type: "error", code: "recording_save_failed", message: "保存できませんでした"};
  assert.equal(end.disabled, true, "normal ended session cannot end again");
  socket.emit(saveError);
  assert.equal(end.disabled, false);
  end.dispatchEvent(new Event("click"));
  assert.equal(warning.textContent, "", "retry clears the old save warning");
  socket.emit({type: "session", state: "ended"});
  socket.close();
  assert.equal(end.disabled, true, "disconnected controls stay disabled");
  timers.find(timer => timer.delay === 2000).fn();
  socket = sockets.at(-1);
  socket.readyState = sandbox.WebSocket.OPEN;
  socket.emit({type: "joined", session_state: "ended", recording: true});
  assert.equal(end.disabled, false, "lost retry result must not prevent another retry");
  end.dispatchEvent(new Event("click"));
  assert.deepEqual(sent, [
    {type: "control", action: "end"},
    {type: "control", action: "end"},
  ]);
  socket.emit(saveError);
  if (__OTHER_WARNING__) {
    socket.emit({type: "error", code: "recording_pending", message: "授業記録の保存を待っています"});
    end.dispatchEvent(new Event("click"));
    assert.equal(warning.textContent, "サーバーからの警告: 授業記録の保存を待っています");
  }
  socket.emit({type: "session", state: __NEXT_STATE__});
  assert.equal(warning.textContent, __OTHER_WARNING__ ? "サーバーからの警告: 授業記録の保存を待っています" : "");
  socket.emit({type: "session", state: "ended"});
  assert.equal(end.disabled, true, "new session clears the previous save failure");
'''.replace("__NEXT_STATE__", json.dumps(next_state))
       .replace("__OTHER_WARNING__", json.dumps(other_warning)))
