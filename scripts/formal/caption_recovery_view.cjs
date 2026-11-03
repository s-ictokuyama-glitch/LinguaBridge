"use strict";

const fs = require("node:fs");
const { CaptionRecovery } = require("../../web/caption-recovery.js");

const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const observations = cases.map(({ language, frames }) => {
  const recovery = new CaptionRecovery();
  for (const frame of frames) {
    if (frame.type === "joined") recovery.applyHistoryFloor(frame.history_from);
    if (frame.type === "caption") recovery.accept(frame, language);
  }
  return { last_seq: recovery.lastSeq, cards: recovery.captions };
});
process.stdout.write(JSON.stringify(observations));
