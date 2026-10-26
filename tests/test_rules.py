from bbb_companion.models import ChatSettings, LeaveSettings
from bbb_companion.rules import ChatDetector, LeaveDetector, Message


def msg(i, text="Здравствуйте", at=100, author=None, name=None):
    return Message(str(i), author or f"user{i}", name or f"Человек {i}", text, at)


def test_unique_authors_variants_and_self_exclusion():
    detector = ChatDetector(ChatSettings())
    assert not detector.add(msg(0, author="own"), {"own"}, set())
    assert not detector.add(msg(1, name="Мой профиль"), set(), {"мой профиль"})
    for i in range(10, 14):
        assert not detector.add(msg(i, author="same"), set(), set())
    assert not detector.add(msg(20, "добрый день!"), set(), set())
    assert not detector.add(msg(21, " ЗДРАВСТВУЙТЕ! "), set(), set())
    assert not detector.add(msg(22, "Доброе утро"), set(), set())
    reactions = detector.add(msg(23), set(), set())
    assert len(reactions) == 1
    assert reactions[0].authors == 5 and reactions[0].total == 5
    assert reactions[0].group == "Приветствие"
    assert not detector.add(msg(24), set(), set())


def test_ratio_counts_all_external_authors_and_duplicate_ids():
    detector = ChatDetector(ChatSettings())
    for i in range(4):
        detector.add(msg(i, "Вопрос"), set(), set())
    for i in range(10, 15):
        assert not detector.add(msg(i, "+"), set(), set())
    assert not detector.add(msg(14, "+"), set(), set())
    reaction = detector.add(msg(15, "++"), set(), set())[0]
    assert reaction.authors == 6 and reaction.total == 10


def test_restored_wave_not_replayed_but_new_wave_is_allowed():
    detector = ChatDetector(ChatSettings())
    for i in range(5):
        detector.add(msg(i, "+", at=100+i), set(), set())
    saved_id = detector.dump()["waves"]["Плюс"]["id"]
    restored = ChatDetector(ChatSettings(), detector.dump())
    for i in range(10, 16):
        assert not restored.add(msg(i, "+", at=110+i), set(), set())
    reactions = []
    for i in range(20, 25):
        reactions += restored.add(msg(i, "++", at=300+i), set(), set())
    assert len(reactions) == 1 and reactions[0].id != saved_id


def test_window_expires():
    detector = ChatDetector(ChatSettings())
    for i in range(4):
        detector.add(msg(i, at=i), set(), set())
    for i in range(5,9):
        assert not detector.add(msg(i, at=100), set(), set())
    assert detector.add(msg(9, at=101), set(), set())


def test_mass_exit_confirmation_recovery_and_disconnect():
    detector = LeaveDetector(LeaveSettings())
    for second in range(10):
        assert detector.sample(second, 90, True) is None
    for second in range(10, 40):
        assert detector.sample(second, 30, True) is None
    assert "90 → 30" in detector.sample(40, 30, True)
    detector = LeaveDetector(LeaveSettings())
    detector.sample(0, 90, True)
    detector.sample(1, 30, True)
    detector.sample(2, 80, True)
    assert detector.candidate is None
    detector.sample(3, None, False)
    assert not detector.samples
    for second in range(4, 40):
        assert detector.sample(second, 20, True) is None


def test_initial_empty_room_not_finished_and_only_bots_left():
    detector = LeaveDetector(LeaveSettings())
    for second in range(50):
        assert detector.sample(second, 0, True) is None
    detector.sample(50, 10, True)
    for second in range(51, 66):
        assert detector.sample(second, 0, True) is None
    assert detector.sample(66, 0, True)


def test_missing_count_and_observation_gap_do_not_trigger_exit():
    detector = LeaveDetector(LeaveSettings())
    detector.sample(0, 100, True)
    assert detector.sample(50, 20, True) is None
    assert detector.candidate is None
    detector.sample(51, None, True)
    assert detector.sample(52, 0, True) is None
