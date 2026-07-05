import type { Segment } from '../types';

export type AcceptResult = {
  /** 新規に受け入れた文（seq 昇順・重複なし） */
  accepted: Segment[];
  /** 受け入れ後の最終 seq。何も受け入れなければ lastSeq のまま */
  newLastSeq: number;
  /**
   * lastSeq と受信バッチ先頭の間に取得できない欠落があった場合、
   * 取得できた最初の seq。欠落がなければ null。
   * （中継のリングバッファから溢れた分を追いかけたときに起こる）
   */
  gapBeforeSeq: number | null;
};

/**
 * 受信バッチを既読位置 lastSeq に突き合わせて、新規分だけを昇順で受け入れる。
 * 重複（seq <= lastSeq）と順序の乱れはここで吸収する。純関数。
 */
export function acceptSegments(lastSeq: number, incoming: Segment[]): AcceptResult {
  const sorted = [...incoming].sort((a, b) => a.seq - b.seq);
  const accepted: Segment[] = [];
  let prev = lastSeq;
  let gapBeforeSeq: number | null = null;

  for (const seg of sorted) {
    if (seg.seq <= prev) continue; // 重複・巻き戻りは捨てる
    if (accepted.length === 0 && seg.seq > lastSeq + 1) {
      gapBeforeSeq = seg.seq;
    }
    accepted.push(seg);
    prev = seg.seq;
  }

  return { accepted, newLastSeq: prev, gapBeforeSeq };
}
