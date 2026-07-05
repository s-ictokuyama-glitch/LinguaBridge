import { describe, expect, it } from 'vitest';
import { acceptSegments } from './segmentStream';
import type { Segment } from '../types';

const seg = (seq: number): Segment => ({ seq, text: `文${seq}`, tMs: 1000 + seq });

describe('acceptSegments', () => {
  it('空バッチは何も受け入れず lastSeq を維持する', () => {
    const r = acceptSegments(5, []);
    expect(r.accepted).toEqual([]);
    expect(r.newLastSeq).toBe(5);
    expect(r.gapBeforeSeq).toBeNull();
  });

  it('新規の文を seq 昇順で受け入れる', () => {
    const r = acceptSegments(0, [seg(1), seg(2), seg(3)]);
    expect(r.accepted.map((s) => s.seq)).toEqual([1, 2, 3]);
    expect(r.newLastSeq).toBe(3);
    expect(r.gapBeforeSeq).toBeNull();
  });

  it('既読分と重複する再送は捨てる（冪等）', () => {
    const r = acceptSegments(2, [seg(1), seg(2), seg(3), seg(4)]);
    expect(r.accepted.map((s) => s.seq)).toEqual([3, 4]);
    expect(r.newLastSeq).toBe(4);
  });

  it('バッチ内の重複も1つにまとめる', () => {
    const r = acceptSegments(0, [seg(1), seg(1), seg(2)]);
    expect(r.accepted.map((s) => s.seq)).toEqual([1, 2]);
  });

  it('順序が乱れて届いても昇順に直す', () => {
    const r = acceptSegments(0, [seg(3), seg(1), seg(2)]);
    expect(r.accepted.map((s) => s.seq)).toEqual([1, 2, 3]);
  });

  it('既読位置との間に欠落があれば gapBeforeSeq で報告する', () => {
    // 既読 seq=3 に対し、リングバッファ溢れで seq=10 以降しか取れなかったケース
    const r = acceptSegments(3, [seg(10), seg(11)]);
    expect(r.accepted.map((s) => s.seq)).toEqual([10, 11]);
    expect(r.gapBeforeSeq).toBe(10);
  });

  it('連番どおりに続いていれば欠落なしと判定する', () => {
    const r = acceptSegments(3, [seg(4), seg(5)]);
    expect(r.gapBeforeSeq).toBeNull();
  });

  it('新規参加 (lastSeq=0) でバッファ先頭が seq=1 でない場合も欠落として報告する', () => {
    const r = acceptSegments(0, [seg(50), seg(51)]);
    expect(r.gapBeforeSeq).toBe(50);
  });
});
