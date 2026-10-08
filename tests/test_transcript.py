"""Meeting transcripts (.vtt, .srt): speakers are found by where they stand, the masked copy keeps
its timings, and speaker labels in pasted text are cues for names too."""
import pytest

from optiv_pii_shield import Settings, run
from optiv_pii_shield.detect.structure import structure_findings
from optiv_pii_shield.extract import extract, sniff
from optiv_pii_shield.extract import transcript as tr
from optiv_pii_shield.guard import Guard
from optiv_pii_shield.models import Span

VTT = """WEBVTT - Fab 3 yield review with Dana Whitlock

NOTE recorded by Marcus Oyelaran
on 14 March

STYLE
::cue { color: white }

intro
00:00:01.000 --> 00:00:05.500
<v Dana Whitlock>Thanks for joining. Line 4 yield fell
to 86.7% after the recipe change.</v>

00:00:06.000 --> 00:00:11.250
<v Marcus Oyelaran>I think ET-07 is drifting. Ines, can you pull the logs?

00:00:12.000 --> 00:00:15.000
<v Speaker 3>Yes. Mail me at ines.carvalho@northfab.example or call (408) 555-0177.

00:00:16.000 --> 00:00:18.000
Whitlock will brief the VP on Friday.
"""
SRT = """1
00:00:01,000 --> 00:00:04,000
Dana Whitlock: Thanks for joining, everyone.

2
00:00:04,500 --> 00:00:09,000
Marcus Oyelaran: ET-07 is drifting.
Oyelaran here, for the record.

3
00:00:09,500 --> 00:00:12,000
- Moderator: Any questions for Dana?
"""
VALUES = ["Dana", "Whitlock", "Marcus", "Oyelaran", "ines.carvalho@northfab.example", "(408) 555-0177"]


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("transcripts")
    (d / "review.vtt").write_text(VTT, encoding="utf-8")
    (d / "review.srt").write_text(SRT, encoding="utf-8", newline="\r\n")
    return d


def test_files_are_read_as_transcripts_and_written_back_unchanged(files):
    for name, text in (("review.vtt", VTT), ("review.srt", SRT)):
        assert sniff(files / name) == "transcript"
        assert tr.build(tr.read(files / name), {}) == text


def test_cues_speakers_and_the_text_around_them(files):
    doc = extract(files / "review.vtt")
    assert doc.file_type == "transcript" and doc.structure == {"cues": 4, "speakers": 3}
    speakers = [(s.text, s.location) for s in doc.spans if s.kind == "speaker"]
    assert speakers == [("Dana Whitlock", "cue 1 at 00:00:01.000, speaker"), ("Marcus Oyelaran", "cue 2 at 00:00:06.000, speaker"),
                        ("Speaker 3", "cue 3 at 00:00:12.000, speaker")]
    said = {s.location: s.text for s in doc.spans if s.kind == "paragraph"}
    assert said["cue 1 at 00:00:01.000"] == "Thanks for joining. Line 4 yield fell\nto 86.7% after the recipe change.</v>"
    assert said["transcript title"].strip() == "- Fab 3 yield review with Dana Whitlock"
    assert [t for loc, t in said.items() if loc == "transcript note"] and "Whitlock will brief the VP on Friday." in said.values()
    assert not any("-->" in s.text or "::cue" in s.text for s in doc.spans)
    assert "**Dana Whitlock:** Thanks for joining." in doc.markdown


def test_speaker_field_is_a_name_and_a_role_is_not():
    def found(text, kind="speaker"):
        return [(f.text, f.score) for f in structure_findings(Span(id="s", file="f", text=text, kind=kind), set())]

    assert found("Dana Whitlock") == [("Dana Whitlock", 0.8)] and found("  Ines ") == [("Ines", 0.8)]
    assert found("Speaker 3") == [] and found("Moderator") == [] and found("Interviewer #2") == []
    # in running text: a label that speaks twice, or one that starts with a listed given name
    lines = ("Dana Whitlock: we slip two weeks.\n[10:32] Marcus: agreed.\nNext Steps: none.\nAgenda: yield\n"
             "Dana Whitlock: thanks.\nPriya (10:40): ok")
    assert [t for t, _ in found(lines, "paragraph")] == ["Dana Whitlock", "Dana Whitlock", "Priya"]


def test_transcripts_are_redacted_and_the_masked_copies_keep_their_timings(files, tmp_path):
    res = run([files / "review.vtt", files / "review.srt"], Settings(), tmp_path)
    assert not res.errors
    for name, original in (("review.vtt", VTT), ("review.srt", SRT)):
        masked = (tmp_path / name.replace(".", ".masked.")).read_text(encoding="utf-8")
        assert not [v for v in VALUES if v in masked + res.redacted[name]], name
        assert [ln for ln in masked.split("\n") if "-->" in ln] == [ln for ln in original.split("\n") if "-->" in ln]
        assert masked.count("\n") == original.count("\n")
    vtt = (tmp_path / "review.masked.vtt").read_text(encoding="utf-8")
    assert vtt.startswith("WEBVTT - Fab 3 yield review with [PERSON_") and "<v [PERSON_" in vtt and "<v Speaker 3>" in vtt
    assert "86.7%" in vtt and "::cue { color: white }" in vtt
    # the same person carries the same token in both files, and where only the surname is said
    token = {f.text: f.token for fs in res.findings.values() for f in fs if f.decision == "redact" and f.entity_type == "PERSON"}
    assert token["Dana Whitlock"] == token["Whitlock"] == token["Dana"] and token["Marcus Oyelaran"] == token["Oyelaran"]
    srt = (tmp_path / "review.masked.srt").read_text(encoding="utf-8")
    assert f"{token['Dana Whitlock']}: Thanks for joining" in srt and "- Moderator: Any questions for [PERSON_" in srt
    layers = {f.layer for f in res.findings["review.vtt"] if f.text == "Dana Whitlock"}
    assert any(layer.startswith("L3") or "L4" in layer for layer in layers)


def test_pasted_transcript_is_handled_by_the_guard():
    r = Guard().check("Summarise this call:\n\nDana Whitlock: we slip two weeks if yield stays low.\nMarcus: I will pull the logs.\n"
                      "Dana Whitlock: thanks, Marcus.")
    assert r["verdict"] == "redacted" and "Dana" not in r["safe_text"] and "Marcus" not in r["safe_text"]
    assert r["safe_text"].count("[PERSON_001]:") == 2 and "thanks, [PERSON_002]." in r["safe_text"]
