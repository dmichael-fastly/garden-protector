"""The synthetic scene round-trip is load-bearing for the offline e2e harness:
render_scene(s) -> JPEG -> classify_jpeg() must recover the right verdict for s.
"""
import pytest

from hardware import scenes


@pytest.mark.parametrize("scene", scenes.SCENE_NAMES)
def test_render_produces_decodable_jpeg(scene):
    jpeg = scenes.render_scene(scene)
    assert jpeg[:2] == b"\xff\xd8", "JPEG SOI marker"
    assert len(jpeg) > 200


@pytest.mark.parametrize("scene", scenes.SCENE_NAMES)
def test_scene_round_trips_to_its_verdict_action(scene):
    jpeg = scenes.render_scene(scene)
    verdict = scenes.classify_jpeg(jpeg)
    expected = scenes.verdict_for_scene(scene)
    assert verdict["action"] == expected["action"], (
        f"scene {scene!r} should classify as {expected['action']}, got {verdict}")


def test_human_scene_is_a_veto():
    v = scenes.classify_jpeg(scenes.render_scene("human"))
    assert v["action"] == "none"
    assert v.get("reason") == "human"


def test_critter_scenes_mitigate():
    for scene in ("raccoon", "fox", "cat"):
        v = scenes.classify_jpeg(scenes.render_scene(scene))
        assert v["action"] == "mitigate", scene


def test_classify_garbage_is_failsafe_none():
    # undecodable bytes -> stand down (never a spray)
    assert scenes.classify_jpeg(b"not a jpeg")["action"] == "none"
