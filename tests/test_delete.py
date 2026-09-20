"""取得したファイルの削除。

消す操作は取り返しがつかないので、消してよいものだけを消せているかを確かめる。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module, state, webui  # noqa: E402


class DeleteWorkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.games = self.root / "games"
        self.games.mkdir()

        self.config = config_module.Config()
        self.config.install_dir = str(self.games)
        self.config.install_dirs = [str(self.games)]
        self.config.state_file = str(self.root / "state.json")
        # 既定のままだとプロジェクト直下の downloads/ を触ってしまう
        self.config.download_dir = str(self.root / "downloads")

        self.backend = webui.Backend(self.config)

    def _add(self, work_id: str, *, registered: bool = False,
             directory: Path | None = None, files: int = 3) -> Path:
        target = directory if directory is not None else self.games / work_id
        target.mkdir(parents=True, exist_ok=True)
        for index in range(files):
            (target / f"file{index}.bin").write_bytes(b"x" * 1000)

        current = self.backend.state()
        current.works[work_id] = state.InstalledWork(
            id=work_id, title=f"作品{work_id}", directory=str(target),
            steam_app_id=12345 if registered else None,
        )
        current.save()
        return target

    def test_unregistered_work_is_removed(self):
        target = self._add("RJ1")
        result = self.backend.delete_work("RJ1")

        self.assertFalse(target.exists(), "ディレクトリが残っている")
        self.assertIsNone(self.backend.state().get("RJ1"), "記録が残っている")
        self.assertEqual(result["freed"], 3000)

    def test_registered_work_is_refused(self):
        target = self._add("RJ2", registered=True)
        with self.assertRaises(webui.UiError) as caught:
            self.backend.delete_work("RJ2")

        self.assertIn("解除", str(caught.exception))
        self.assertTrue(target.exists(), "拒否したのに消えている")
        self.assertIsNotNone(self.backend.state().get("RJ2"))

    def test_directory_outside_the_install_dirs_is_refused(self):
        """設定を書き換えて別の場所を指させても消さないこと。"""
        outside = self.root / "たいせつなもの"
        target = self._add("RJ3", directory=outside)

        with self.assertRaises(webui.UiError) as caught:
            self.backend.delete_work("RJ3")

        self.assertIn("インストール先の外", str(caught.exception))
        self.assertTrue(target.exists())

    def test_install_root_itself_is_refused(self):
        self._add("RJ4", directory=self.games, files=1)
        with self.assertRaises(webui.UiError):
            self.backend.delete_work("RJ4")
        self.assertTrue(self.games.exists())

    def test_missing_directory_still_clears_the_record(self):
        """フォルダを手で消してあっても、記録は片付けられること。"""
        target = self._add("RJ5")
        import shutil

        shutil.rmtree(target)
        result = self.backend.delete_work("RJ5")

        self.assertIsNone(self.backend.state().get("RJ5"))
        self.assertIn("既にありません", result["message"])

    def test_unknown_work_is_reported(self):
        with self.assertRaises(webui.UiError):
            self.backend.delete_work("RJ999")

    def test_related_links_are_dropped(self):
        """DLC の結び付けを残すと、消えた作品を指したままになる。"""
        self._add("RJ6")
        self._add("RJ7")
        current = self.backend.state()
        current.links.append(state.LinkRecord(source_id="RJ6", target_id="RJ7"))
        current.links.append(state.LinkRecord(source_id="RJ8", target_id="RJ9"))
        current.save()

        result = self.backend.delete_work("RJ6")
        after = self.backend.state()

        self.assertEqual(len(after.links), 1)
        self.assertEqual(after.links[0].source_id, "RJ8")
        self.assertIn("DLC", result["message"])

    def test_other_works_are_untouched(self):
        keep = self._add("RJ10")
        self._add("RJ11")
        self.backend.delete_work("RJ11")

        self.assertTrue(keep.exists())
        self.assertIsNotNone(self.backend.state().get("RJ10"))

    def test_state_file_stays_valid(self):
        self._add("RJ12")
        self._add("RJ13")
        self.backend.delete_work("RJ12")

        payload = json.loads(Path(self.config.state_path).read_text(encoding="utf-8"))
        self.assertEqual([w["id"] for w in payload["works"]], ["RJ13"])


class SteamStateTest(unittest.TestCase):
    def test_reports_a_boolean(self):
        with tempfile.TemporaryDirectory() as name:
            cfg = config_module.Config()
            cfg.state_file = str(Path(name) / "state.json")
            result = webui.Backend(cfg).steam_state()

        self.assertIn("running", result)
        self.assertIsInstance(result["running"], bool)



class DeletePreviewTest(DeleteWorkTest):
    """削除前の下見。何が消えて何が残るかを先に知らせる。"""

    def test_reports_sizes(self):
        self._add("RJ20")
        preview = self.backend.delete_preview("RJ20")

        self.assertEqual(preview["title"], "作品RJ20")
        self.assertIn("KiB", preview["size_label"])
        self.assertFalse(preview["registered"])
        self.assertEqual(preview["applied_to"], [])

    def test_reports_where_the_dlc_was_applied(self):
        """適用済みの DLC を消すと、重ねたファイルは相手側に残る。

        記録だけ黙って消えると、相手の状態が追えなくなる。
        """
        self._add("DLC")
        self._add("BASE")
        current = self.backend.state()
        current.works["BASE"].title = "本編"
        current.links.append(state.LinkRecord(
            source_id="DLC", target_id="BASE", applied_at="2026-08-18T00:00:00+00:00"))
        current.save()

        preview = self.backend.delete_preview("DLC")
        self.assertEqual([x["title"] for x in preview["applied_to"]], ["本編"])

    def test_unapplied_link_is_not_reported(self):
        self._add("DLC2")
        self._add("BASE2")
        current = self.backend.state()
        current.links.append(state.LinkRecord(source_id="DLC2", target_id="BASE2"))
        current.save()

        self.assertEqual(self.backend.delete_preview("DLC2")["applied_to"], [])

    def test_registered_work_is_flagged(self):
        self._add("RJ21", registered=True)
        self.assertTrue(self.backend.delete_preview("RJ21")["registered"])


class DeleteCacheTest(DeleteWorkTest):
    """ダウンロードしたアーカイブも片付けること。

    既定では展開後に消えているが、「アーカイブを残す」設定や、途中で失敗した
    取得の分が残ることがある。
    """

    def test_cache_directory_is_removed(self):
        self._add("RJ30")
        cache = Path(self.config.download_dir) / "RJ30"
        cache.mkdir(parents=True)
        (cache / "RJ30.zip").write_bytes(b"z" * 5000)

        result = self.backend.delete_work("RJ30")

        self.assertFalse(cache.exists(), "キャッシュが残っている")
        self.assertEqual(result["cache_freed"], 5000)
        self.assertEqual(result["freed"], 3000 + 5000)

    def test_missing_cache_is_fine(self):
        self._add("RJ31")
        result = self.backend.delete_work("RJ31")
        self.assertEqual(result["cache_freed"], 0)

    def test_other_works_cache_is_kept(self):
        self._add("RJ32")
        self._add("RJ33")
        keep = Path(self.config.download_dir) / "RJ33"
        keep.mkdir(parents=True)
        (keep / "a.zip").write_bytes(b"z" * 100)

        self.backend.delete_work("RJ32")
        self.assertTrue(keep.exists(), "無関係のキャッシュを消している")

    def test_message_mentions_files_left_behind(self):
        self._add("DLC3")
        self._add("BASE3")
        current = self.backend.state()
        current.works["BASE3"].title = "本編3"
        current.links.append(state.LinkRecord(
            source_id="DLC3", target_id="BASE3", applied_at="2026-08-18T00:00:00+00:00"))
        current.save()

        result = self.backend.delete_work("DLC3")
        self.assertEqual(result["left_behind"], ["本編3"])
        self.assertIn("本編3", result["message"])
        self.assertIn("残っています", result["message"])


class SessionKindTest(unittest.TestCase):
    """ゲームモードかどうかの判定。

    ゲームモードでは Steam を終了できない (Steam 自身がセッションで、この
    ツールもそこから起動されている)。「終了してください」と案内しても
    実行できないので、区別して案内を変える必要がある。
    """

    def setUp(self):
        from dlsite_deck import steam

        self.steam = steam

    def _kind(self, commands):
        # 判定そのものを試す。実行中の OS に左右されないようにするため。
        return self.steam.session_from_commands(set(commands))

    def test_gamescope_means_gaming_mode(self):
        self.assertEqual(
            self._kind({"gamescope", "steam", "steamwebhelper"}),
            self.steam.SESSION_GAMING,
        )

    def test_gamescope_session_variant(self):
        self.assertEqual(
            self._kind({"gamescope-session", "steam"}), self.steam.SESSION_GAMING
        )

    def test_plasma_means_desktop_mode(self):
        # 実機の Desktop Mode で動いていた顔ぶれ
        self.assertEqual(
            self._kind({"plasmashell", "kwin_wayland", "startplasma-way", "steam"}),
            self.steam.SESSION_DESKTOP,
        )

    def test_gaming_mode_wins_when_both_appear(self):
        """判断に迷う顔ぶれでは、操作を塞ぐ側に倒す。"""
        self.assertEqual(
            self._kind({"gamescope", "plasmashell"}), self.steam.SESSION_GAMING
        )

    def test_neither_is_unknown(self):
        self.assertEqual(self._kind({"bash", "sshd"}), self.steam.SESSION_UNKNOWN)

    def test_no_processes_is_unknown(self):
        self.assertEqual(self._kind(set()), self.steam.SESSION_UNKNOWN)


class BlockedMessageTest(unittest.TestCase):
    """操作を塞いだときの案内が、モードによって変わること。"""

    def setUp(self):
        from dlsite_deck import steam

        self.steam = steam
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "state.json")
        self.backend = webui.Backend(cfg)

        self.real = steam.session_kind
        self.addCleanup(setattr, steam, "session_kind", self.real)

    def test_gaming_mode_tells_you_to_switch(self):
        self.steam.session_kind = lambda: self.steam.SESSION_GAMING
        message = self.backend._steam_blocked("登録")

        self.assertIn("Desktop Mode", message)
        # 実行できない指示を出さないこと
        self.assertNotIn("終了してから", message)

    def test_desktop_mode_tells_you_to_quit_steam(self):
        self.steam.session_kind = lambda: self.steam.SESSION_DESKTOP
        message = self.backend._steam_blocked("登録")

        self.assertIn("終了", message)
        self.assertNotIn("Desktop Mode", message)

    def test_steam_state_reports_whether_steam_can_be_quit(self):
        self.steam.session_kind = lambda: self.steam.SESSION_GAMING
        self.assertFalse(self.backend.steam_state()["can_quit_steam"])

        self.steam.session_kind = lambda: self.steam.SESSION_DESKTOP
        self.assertTrue(self.backend.steam_state()["can_quit_steam"])


class RegistrationDetectionTest(unittest.TestCase):
    """Steam に登録済みかどうかの判別。

    取得しただけの作品と Steam に載せた作品が、一覧で見分けられないという
    問題があった。記録 (state.json) だけを見ると Steam 側で直接削除された
    ときに古いまま残るので、実ファイルと突き合わせる。
    """

    def _entry(self, app_id=None, title="ゲーム", steam_title=None):
        return state.InstalledWork(
            id="RJ1", title=title, directory="/games/x",
            steam_app_id=app_id, steam_title=steam_title,
        )

    def test_matches_by_app_id(self):
        shortcuts = ({3394802138, -900165158}, {"別の名前"}, set())
        self.assertTrue(
            webui._is_registered(self._entry(app_id=3394802138), shortcuts)
        )

    def test_matches_by_name_when_the_id_drifted(self):
        """Steam 側で改名されると AppID が変わる。名前でも照合する。"""
        shortcuts = ({999}, {"ゲーム"}, set())
        self.assertTrue(webui._is_registered(self._entry(app_id=12345), shortcuts))

    def test_uses_the_steam_title_when_set(self):
        shortcuts = ({999}, {"登録名"}, set())
        entry = self._entry(app_id=None, title="DLsite名", steam_title="登録名")
        self.assertTrue(webui._is_registered(entry, shortcuts))

    def test_not_registered_is_false(self):
        shortcuts = ({999}, {"よそのゲーム"}, set())
        self.assertFalse(webui._is_registered(self._entry(), shortcuts))

    def test_stale_record_is_detected(self):
        """Steam 側で消されたのに記録が残っている場合。

        ここを記録任せにすると「登録済み」と出続け、削除も拒否され続ける。
        """
        shortcuts = (set(), set(), set())
        self.assertFalse(
            webui._is_registered(self._entry(app_id=3394802138), shortcuts)
        )

    def test_unknown_when_steam_is_unavailable(self):
        """Steam が見つからない環境で「未登録」と言い切らないこと。"""
        self.assertIsNone(webui._is_registered(self._entry(), None))
        # 記録があるなら、少なくとも登録したことは分かる
        self.assertTrue(webui._is_registered(self._entry(app_id=1), None))

    def test_not_installed_is_false(self):
        self.assertFalse(webui._is_registered(None, (set(), set(), set())))


class RegisteredShortcutsTest(unittest.TestCase):
    """実際の shortcuts.vdf から登録済みを集められること。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.userdata = root / "userdata"
        config = self.userdata / "10000000" / "config"
        config.mkdir(parents=True)

        from dlsite_deck import steam

        # 動いている環境のパス表記にする。別 OS のパスは「よそのもの」として
        # 除かれる仕様なので、そのままだと自分の登録が数えられない。
        local_exe = r'"C:\games\a.exe"' if sys.platform == "win32" else '"/games/a.exe"'
        local_dir = r'"C:\games"' if sys.platform == "win32" else '"/games"'

        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, steam.Shortcut(
            app_name="登録済みゲーム", exe=local_exe, start_dir=local_dir))
        # 別 OS のもの。これは数えてはいけない。
        steam.upsert_shortcut(document, steam.Shortcut(
            app_name="よその環境のゲーム",
            exe='"/other/os/b.exe"' if sys.platform == "win32" else r'"D:\other\b.exe"',
            start_dir='"/other/os"' if sys.platform == "win32" else r'"D:\other"'))
        steam.save_shortcuts(config / "shortcuts.vdf", document, backup=False)

        cfg = config_module.Config()
        cfg.state_file = str(root / "state.json")
        cfg.steam_userdata_dir = str(self.userdata)
        self.backend = webui.Backend(cfg)

    def test_collects_names_and_ids(self):
        found = self.backend.registered_shortcuts()
        self.assertIsNotNone(found)
        app_ids, names, _foreign = found
        self.assertIn("登録済みゲーム", names)
        self.assertTrue(app_ids)

    def test_signed_and_unsigned_ids_both_present(self):
        app_ids, _names, _foreign = self.backend.registered_shortcuts()
        for app_id in list(app_ids):
            partner = app_id + 2**32 if app_id < 0 else app_id - 2**32
            if partner > 0 or app_id > 0:
                continue
        # 符号付きと符号なしの両方が入っていること
        self.assertTrue(any(a < 0 for a in app_ids) or any(a > 2**31 for a in app_ids))

    def test_foreign_entries_are_separated(self):
        """別 OS のパスを持つものは、こちらの登録として数えない。

        Steam はデバイス間で非 Steam ゲームの情報を見せることがある。数えると
        動かないものを「登録済み」と扱ってしまう。
        """
        _app_ids, names, foreign = self.backend.registered_shortcuts()
        self.assertIn("登録済みゲーム", names)
        self.assertNotIn("よその環境のゲーム", names, "別環境のものを数えている")
        self.assertIn("よその環境のゲーム", foreign)

    def test_foreign_duplicate_blocks_registration(self):
        """同名の別環境エントリがあるとき、登録すると相手を壊す。"""
        entry = state.InstalledWork(
            id="RJ1", title="よその環境のゲーム", directory=str(self.tmp.name))
        shortcuts = self.backend.registered_shortcuts()

        self.assertTrue(webui._is_foreign(entry, shortcuts))
        with self.assertRaises(webui.UiError) as caught:
            self.backend._refuse_if_foreign(entry, "登録")
        self.assertIn("別の環境", str(caught.exception))

    def test_local_entry_is_not_blocked(self):
        entry = state.InstalledWork(
            id="RJ2", title="登録済みゲーム", directory=str(self.tmp.name))
        self.backend._refuse_if_foreign(entry, "登録")  # 例外が出なければよい

    def test_missing_steam_returns_none(self):
        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "s.json")
        cfg.steam_userdata_dir = str(Path(self.tmp.name) / "ない")
        self.assertIsNone(webui.Backend(cfg).registered_shortcuts())


class PathWarningTest(unittest.TestCase):
    """設定した置き場が実在しないときの知らせ。

    SteamDeck では展開先を SD カードに置くのが普通で、カードが外れていると
    導入済みの作品が一斉に「記録のみ」になる。取り直しを促す表示のまま
    気付かずに 60GB 取り直す、という事故を防ぐための知らせ。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.games = self.root / "games"
        self.games.mkdir()

        self.config = config_module.Config()
        self.config.install_dir = str(self.games)
        self.config.install_dirs = [str(self.games)]
        self.config.download_dir = str(self.root / "downloads")
        Path(self.config.download_dir).mkdir()
        self.config.state_file = str(self.root / "state.json")
        self.backend = webui.Backend(self.config)

    def _record(self, work_id: str, directory: str) -> None:
        current = self.backend.state()
        current.works[work_id] = state.InstalledWork(
            id=work_id, title=f"作品{work_id}", directory=directory)
        current.save()

    def test_nothing_to_say_when_everything_exists(self):
        result = self.backend.path_warnings()
        self.assertEqual(result["missing"], [])
        self.assertFalse(result["suggest_removable"])

    def test_missing_install_dir_is_reported(self):
        gone = self.root / "はずれたカード"
        self.config.install_dirs = [str(self.games), str(gone)]

        result = self.backend.path_warnings()
        paths = [m["path"] for m in result["missing"]]
        self.assertEqual(paths, [str(gone)])
        self.assertEqual(result["missing"][0]["label"], "インストール先")

    def test_missing_cache_dir_is_reported(self):
        self.config.download_dir = str(self.root / "ないキャッシュ")
        labels = [m["label"] for m in self.backend.path_warnings()["missing"]]
        self.assertIn("キャッシュ", labels)

    def test_counts_record_only_entries_under_the_missing_path(self):
        gone = self.root / "sd" / "dlsite_games"
        self.config.install_dirs = [str(gone)]
        self._record("RJ1", str(gone / "game-a"))
        self._record("RJ2", str(gone / "game-b"))
        # 無関係な場所のものは数えない
        self._record("RJ3", str(self.root / "よそ" / "game-c"))

        found = self.backend.path_warnings()["missing"][0]
        self.assertEqual(found["record_only"], 2)

    def test_removable_looking_path_suggests_the_card(self):
        """SD カードの置き場に見えて、記録のみが出ているなら、まず外れを疑う。"""
        # 実在するカードのラベルと衝突しないよう、確実に無い名前を使う
        gone = Path("/run/media/deck/__no_such_card__/dlsite_games")
        self.config.install_dirs = [str(gone)]
        self._record("RJ1", str(gone / "game-a"))

        result = self.backend.path_warnings()
        self.assertTrue(result["missing"][0]["removable"])
        self.assertTrue(result["suggest_removable"])

    def test_ordinary_path_does_not_blame_the_card(self):
        gone = self.root / "ふつうの場所"
        self.config.install_dirs = [str(gone)]
        self._record("RJ1", str(gone / "game-a"))

        result = self.backend.path_warnings()
        self.assertFalse(result["missing"][0]["removable"])
        self.assertFalse(result["suggest_removable"])

    def test_removable_path_without_records_does_not_suggest(self):
        """まだ何も入れていないなら、カードの話を持ち出さない。"""
        self.config.install_dirs = ["/run/media/deck/__no_such_card__/dlsite_games"]
        self.assertFalse(self.backend.path_warnings()["suggest_removable"])

    def test_library_payload_carries_the_warning(self):
        self.config.install_dirs = [str(self.root / "ない")]
        self.backend.library = lambda refresh=False: ([], [])
        self.assertIn("paths", self.backend.library_json())


class DefaultOrderTest(unittest.TestCase):
    """既定の並び。

    指定された順:
      Steam未登録 → 更新あり → 未取得 → 登録済み・更新不明 → 記録のみ → 別環境と重複

    画面の「状態」順とは意図が違う (あちらは状態の重さ、こちらは次にやること)
    ので、別に持っている。
    """

    def _item(self, **over):
        base = {
            "id": "RJ1", "title": "作品", "category": "game",
            "installed": True, "outdated": False, "update_unknown": False,
            "steam_registered": True, "steam_foreign": False,
            "status_label": "導入済み",
        }
        base.update(over)
        return base

    def test_the_six_ranks_are_in_the_requested_order(self):
        cases = [
            ("Steam未登録", self._item(steam_registered=False)),
            ("更新あり", self._item(outdated=True)),
            ("未取得", self._item(installed=False, status_label="未取得")),
            ("登録済み", self._item()),
            ("更新不明", self._item(update_unknown=True)),
            ("記録のみ", self._item(installed=False, status_label="記録のみ")),
            ("別環境と重複", self._item(steam_foreign=True)),
        ]
        ranks = {label: webui._default_rank(item) for label, item in cases}

        self.assertLess(ranks["Steam未登録"], ranks["更新あり"])
        self.assertLess(ranks["更新あり"], ranks["未取得"])
        self.assertLess(ranks["未取得"], ranks["登録済み"])
        # 更新不明は登録済みと同じ段
        self.assertEqual(ranks["登録済み"], ranks["更新不明"])
        self.assertLess(ranks["登録済み"], ranks["記録のみ"])
        self.assertLess(ranks["記録のみ"], ranks["別環境と重複"])

    def test_unregistered_wins_over_outdated(self):
        """両方に当てはまるときは、より先の段に入ること。"""
        both = self._item(steam_registered=False, outdated=True)
        self.assertEqual(webui._default_rank(both),
                         webui._default_rank(self._item(steam_registered=False)))

    def test_foreign_wins_over_everything(self):
        """触れないものは、他に何が重なっていても最後。"""
        item = self._item(steam_foreign=True, outdated=True, steam_registered=False)
        self.assertEqual(webui._default_rank(item), webui._DEFAULT_RANK["foreign"])

    def test_non_game_is_not_treated_as_unregistered(self):
        """マンガや音声に Steam 登録の概念は無い。"""
        manga = self._item(category="manga", steam_registered=False)
        self.assertEqual(webui._default_rank(manga), webui._DEFAULT_RANK["done"])

    def test_titles_are_sorted_within_a_rank(self):
        import tempfile as tf

        with tf.TemporaryDirectory() as name:
            cfg = config_module.Config()
            cfg.state_file = str(Path(name) / "state.json")
            backend = webui.Backend(cfg)
            backend.library = lambda refresh=False: ([], [])
            self.assertIn("items", backend.library_json())

if __name__ == "__main__":
    unittest.main()
