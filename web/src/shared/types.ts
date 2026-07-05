/** 中継を流れる唯一のデータ。日本語の確定文1つ分 */
export type Segment = {
  /** ルーム内で単調増加する連番。重複排除・欠落検出のキー */
  seq: number;
  /** 日本語確定文 */
  text: string;
  /** 先生端末での確定時刻 (epoch ms)。遅延計測用 */
  tMs: number;
};

export type CreateRoomResponse = {
  roomId: string;
  teacherToken: string;
};

export type PollResponse = {
  segments: Segment[];
  latestSeq: number;
  /** false ならルームは終了済み（または存在しない） */
  active: boolean;
};

export type TransportStatus = 'connected' | 'reconnecting' | 'closed';
