import type { CreateRoomResponse, Segment, TransportStatus } from '../types';

/**
 * 先生→生徒へ確定文を届ける通信層の抽象。
 * GAS ポーリング実装と将来の Cloudflare WebSocket 実装の差し替え境界であり、
 * テストではインメモリフェイクを差し込む主シーム。
 */
export interface RelayTransport {
  /** 先生: ルームを作成する。以後 publish / closeRoom が使える */
  createRoom(): Promise<CreateRoomResponse>;

  /** 先生: 確定文を配信する。seq の採番は呼び出し側の責務 */
  publish(segments: Segment[]): Promise<void>;

  /** 先生: ルームを終了する */
  closeRoom(): Promise<void>;

  /** 生徒: ルームに参加し、sinceSeq より後の文の受信を開始する */
  join(roomId: string, sinceSeq: number): void;

  /** 生徒: 新しい確定文のバッチを受け取る（seq 昇順・重複なし） */
  onSegments(cb: (batch: Segment[]) => void): void;

  /** 生徒: 欠落検出。取得できなかった範囲の直後の seq を通知する */
  onGap(cb: (firstAvailableSeq: number) => void): void;

  /** 接続状態の変化（変化したときだけ通知） */
  onStatus(cb: (status: TransportStatus) => void): void;

  /** 受信停止・後片付け */
  close(): void;
}
