"""生徒用URLに載せるLAN IPの選択（#23）。

`get_lan_ip()` は以前 `8.8.8.8:80` へ UDP connect して既定経路のIFを選んでいた。
実パケットは出ないが「外部アドレスへの connect がゼロ」を機械判定できなくなるため、
インターフェース列挙 + 優先順位ルールへ置き換えた。ここではその優先順位を固定する。

優先順位の根拠: 学校のLANは 192.168.* か 10.* が圧倒的に多い。172.17.* は Docker の
既定ブリッジ、192.168.56.* は VirtualBox のホストオンリー、169.254.* は DHCP 失敗時の
link-local で、いずれも生徒端末からは到達できない。
"""

from __future__ import annotations

import pytest

from server.main import select_lan_ip

LOOPBACK = "127.0.0.1"


class TestSelectLanIp:
    def test_no_candidates_falls_back_to_loopback(self):
        assert select_lan_ip([]) == LOOPBACK

    def test_single_lan_address_is_chosen(self):
        assert select_lan_ip(["192.168.1.42"]) == "192.168.1.42"

    def test_docker_bridge_loses_to_real_lan(self):
        # 列挙順は OS 任せなので、Docker が先に来ても負けること
        assert select_lan_ip(["172.17.0.1", "192.168.1.42"]) == "192.168.1.42"

    def test_link_local_loses_to_real_lan(self):
        assert select_lan_ip(["169.254.10.3", "10.0.5.7"]) == "10.0.5.7"

    def test_virtualbox_host_only_loses_to_real_lan(self):
        assert select_lan_ip(["192.168.56.1", "192.168.1.42"]) == "192.168.1.42"

    def test_class_c_preferred_over_class_a(self):
        assert select_lan_ip(["10.0.5.7", "192.168.1.42"]) == "192.168.1.42"

    def test_class_a_preferred_over_class_b(self):
        assert select_lan_ip(["172.20.0.9", "10.0.5.7"]) == "10.0.5.7"

    @pytest.mark.parametrize(
        "candidates",
        [
            ["172.17.0.1", "192.168.56.1", "169.254.10.3"],
            ["169.254.10.3"],
        ],
    )
    def test_only_unreachable_candidates_still_returns_one(self, candidates):
        # 生徒端末から到達できない候補しか無い場合でも、何かは表示する
        # （先生が「このIPは違う」と気づける方が、127.0.0.1 に落とすより実用的）
        assert select_lan_ip(candidates) == candidates[0]

    def test_loopback_is_never_chosen_over_a_lan_address(self):
        assert select_lan_ip([LOOPBACK, "192.168.1.42"]) == "192.168.1.42"

    def test_public_address_loses_to_private(self):
        # グローバルIPが直付けされたPCでも、LAN側のIPを優先する
        assert select_lan_ip(["203.0.113.9", "192.168.1.42"]) == "192.168.1.42"

    def test_stable_when_two_equally_good_candidates(self):
        # 同点なら列挙順の先頭。実行のたびにURLが変わらないことが運用上重要
        assert select_lan_ip(["192.168.1.42", "192.168.1.99"]) == "192.168.1.42"

    def test_host_octet_one_loses_to_a_dhcp_looking_address(self):
        # VMware/Hyper-V の仮想スイッチは同じ 192.168.* 帯に .1 で現れる。
        # DHCPで配られた実機は .1 を取らないので、これで実LANを選び分けられる
        # （この開発機の実際の候補: 192.168.1.34 / 192.168.74.1 / 192.168.70.1）
        assert select_lan_ip(["192.168.74.1", "192.168.1.34"]) == "192.168.1.34"
        assert select_lan_ip(["192.168.74.1", "192.168.70.1", "192.168.1.34"]) == "192.168.1.34"

    def test_host_octet_one_still_wins_over_a_worse_band(self):
        assert select_lan_ip(["10.0.0.1", "169.254.10.3"]) == "10.0.0.1"


class TestGetLanIp:
    def test_returns_an_ipv4_string(self):
        """実環境で呼んでも例外にならず、IPv4文字列を返す。

        外部connectをしないことは tests/invariants/test_offline_invariants.py が判定する。
        """
        from server.main import get_lan_ip

        ip = get_lan_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)

    def test_falls_back_to_loopback_when_enumeration_fails(self, monkeypatch):
        import socket as socket_mod

        def boom(*_args, **_kwargs):
            raise OSError("名前解決できない環境")

        monkeypatch.setattr(socket_mod, "getaddrinfo", boom)
        monkeypatch.setattr(socket_mod, "gethostname", lambda: "any-host")

        from server.main import get_lan_ip

        assert get_lan_ip() == LOOPBACK
