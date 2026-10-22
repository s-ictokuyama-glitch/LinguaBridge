"""Segment（ASRに投げる音声1単位）から Turn（字幕カード1枚）を組み立てる層（#27）。

VAD の無音長だけで切ると「例えばですね……これは……」が2枚のカードに割れる。
ASR テキストの末尾の文法クラスを見て、文中の境界では切らずに連結する。

  - `boundary`: 末尾テキストの文法クラス分類（差し替え可能なシーム）
  - `assembler`: Segment の ASR 結果と TurnBoundary から Turn を確定する状態機械
"""

from server.turn.assembler import Turn, TurnAssembler
from server.turn.boundary import (
    BoundaryClass,
    BoundaryClassifier,
    SurfaceBoundaryClassifier,
    build_boundary_classifier,
)

__all__ = [
    "BoundaryClass",
    "BoundaryClassifier",
    "SurfaceBoundaryClassifier",
    "Turn",
    "TurnAssembler",
    "build_boundary_classifier",
]
