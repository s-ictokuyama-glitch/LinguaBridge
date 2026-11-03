"use strict";

class CaptionRecovery {
  #lastSeq = 0;
  #captions = [];

  get lastSeq() {
    return this.#lastSeq;
  }

  get captions() {
    return this.#captions;
  }

  applyHistoryFloor(historyFrom) {
    const expiredThrough = historyFrom - 1;
    if (expiredThrough > this.#lastSeq) {
      this.#lastSeq = expiredThrough;
      this.#advance(this.#insertionIndex(historyFrom));
    }
  }

  accept(caption, selectedLang) {
    if (caption.lang !== selectedLang) return null;

    const index = this.#insertionIndex(caption.seq);
    const existing = this.#captions[index];
    if (existing?.seq === caption.seq) {
      if (existing.lang === caption.lang) return null;
      this.#captions[index] = { ...existing, lang: caption.lang, text: caption.text };
      return { kind: "replace", index };
    }

    this.#captions.splice(index, 0, caption);
    this.#advance(index);
    return { kind: "insert", index };
  }

  #insertionIndex(seq) {
    let start = 0;
    let end = this.#captions.length;
    while (start < end) {
      const middle = Math.floor((start + end) / 2);
      if (this.#captions[middle].seq < seq) {
        start = middle + 1;
      } else {
        end = middle;
      }
    }
    return start;
  }

  #advance(index) {
    while (this.#captions[index]?.seq === this.#lastSeq + 1) {
      this.#lastSeq += 1;
      index += 1;
    }
  }
}

if (typeof module !== "undefined") {
  module.exports = { CaptionRecovery };
}
