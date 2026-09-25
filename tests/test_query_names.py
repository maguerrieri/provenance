"""One giver filed under several names is flagged, never merged, and never ranked as two.

Filers do not reliably split or spell out a name. One giver can be 'Rue Quillon'/'' on one row
(the whole name in the last-name field), 'Quillon'/'Rue' on the next, and 'Quillon'/'R' or
'Quillon'/'R M' on a third. The ranking grouped by (last, first), so each was a contributor of
its own, short of what the giver gave: someone smaller could be named "the largest
contributor", a tie could list "Rue Quillon | Rue Quillon", and `contributor_total` asked for
Quillon, Rue left the other rows out. Each value reproduced, so a citation of it verified green.

Three fixes each patched one pair of names, and each left the next pair open. The rule now is
one relation and its closure: two names are linked when an initial could stand for a word
either way round (`_could_be`), and groups are the transitive closure (`_groups`). That
over-merges by construction, so a group's total bounds any real giver in it. A name that could
be a shorter filing of another with no surname in common ('R'/'' for 'Quillon'/'Rue', `_fits`)
counts in that name's group without linking it. Groups are only
ever a safety check: every figure is for a name exactly as filed. A ranking stands only when the
leader's own total is at least every other group's bound and no other name is in the leader's
group; a total only when no other name is in its group. names=as_filed lifts that gate, and
form_type=A lifts only the late-report one.
"""

from __future__ import annotations

import itertools
import random
import re
import zipfile

import pytest

from vgpipe import calaccess, queries
from vgpipe.models import QueryCitation, Source
from vgpipe.verify import verify_query_source

FILER = "9991290"
F460, F497 = "9991291", "9991292"
DAY = " 12:00:00 AM"

RCPT_HEAD = ("FILING_ID\tAMEND_ID\tTRAN_ID\tLINE_ITEM\tCTRIB_NAML\tCTRIB_NAMF\tCTRIB_EMP"
             "\tCTRIB_OCC\tCTRIB_CITY\tCTRIB_ZIP4\tRCPT_DATE\tAMOUNT\tFORM_TYPE\n")
S497_HEAD = ("FILING_ID\tAMEND_ID\tLINE_ITEM\tREC_TYPE\tFORM_TYPE\tTRAN_ID\tENTITY_CD"
             "\tENTY_NAML\tENTY_NAMF\tENTY_CITY\tENTY_ZIP4\tCTRIB_EMP\tCTRIB_OCC\tELEC_DATE"
             "\tCTRIB_DATE\tDATE_THRU\tAMOUNT\tCMTE_ID\n")
FILINGS = ("FILER_ID\tFILING_ID\tFORM_ID\tFILING_DATE\n"
           f"{FILER}\t{F460}\tF460\t7/31/2026{DAY}\n"
           f"{FILER}\t{F497}\tF497\t10/5/2026{DAY}\n")
# The 460 covers the first half of 2026, so a late report after it is pending.
COVERS = ("FILING_ID\tAMEND_ID\tREC_TYPE\tFORM_TYPE\tFILER_ID\tFILER_NAML\tFROM_DATE\tTHRU_DATE"
          "\tELECT_DATE\tCAND_NAML\tCAND_NAMF\tSUP_OPP_CD\n"
          f"{F460}\t0\tCVR\tF460\t{FILER}\tExample Fen Committee\t1/1/2026{DAY}"
          f"\t6/30/2026{DAY}\t11/3/2026{DAY}\t\t\t\n")

WHOLE = ("Rue Quillon", "")     # the whole name in the last-name field
RUE = ("Quillon", "Rue")        # the same name, split
R = ("Quillon", "R")            # a first initial
RM = ("Quillon", "R M")         # a first and a middle initial
ROE = ("Quillon", "Roe")
ADA = ("Quillon", "Ada")
S = ("Quillon", "S")
BARE = ("Quillon", "")
NOBODY = ("", "")
VARDLE = ("Vardle", "Tamsin")


def gift(i, name, amount, where=("", "", "")):
    (last, first), (city, zip4, employer) = name, where
    return (f"{F460}\t0\tA-{i}\t{i}\t{last}\t{first}\t{employer}\t\t{city}\t{zip4}"
            f"\t3/{i % 28 + 1}/2026{DAY}\t{amount}\tA\n")


def late_gift(i, name, amount, where=("", "", "")):
    (last, first), (city, zip4, employer) = name, where
    return (f"{F497}\t0\t1\tS497\tF497P1\tL-{i}\tIND\t{last}\t{first}\t{city}\t{zip4}"
            f"\t{employer}\t\t11/3/2026{DAY}\t10/2/2026{DAY}\t\t{amount}\t\n")


def build(root, gifts, late=(), head=RCPT_HEAD):
    """`gifts` and `late`: (name, amount) or (name, amount, (city, zip, employer))."""
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        # In latin-1, as the export is written and read (calaccess._rows).
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", (head + "".join(
            gift(i, *g) for i, g in enumerate(gifts, 1))).encode("latin-1"))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", (S497_HEAD + "".join(
            late_gift(i, *g) for i, g in enumerate(late, 1))).encode("latin-1"))
    calaccess.build(root)
    return root


def run(root, name, **params):
    return queries.run(f"calaccess.{name}", {"filer_id": FILER, **params}, root)


def cite(root, name, expected, **params):
    s = Source(url=calaccess.filing_url(F460), publisher="California Secretary of State",
               author="California Secretary of State", source_type="official_record",
               date="2026-07-31", snippet="monetary contributions received",
               query=QueryCitation(name=f"calaccess.{name}",
                                   params={"filer_id": FILER, **params}, expected=expected))
    return verify_query_source(s, root).verification


def words(name):
    return queries._name(*name).words


def could(a, b):
    return queries._could_be(queries._name(*a), queries._name(*b))


# --- the relation --------------------------------------------------------------------------


def test_an_initial_could_stand_for_a_word_either_way_round():
    """#129's two pairs: each name is shorter than the other somewhere. One-way fitting (#126)
    left them two givers."""
    assert could(RUE, RM) and could(RM, RUE)
    assert could(("Quillon", "R Mae"), ("Quillon", "Rue M"))
    assert could(R, RUE) and could(WHOLE, RUE) and could(("R Quillon", ""), RUE)
    assert could(BARE, RUE) and could(NOBODY, VARDLE)


def test_two_different_initials_are_not_one_giver():
    assert not could(R, S)
    assert not could(RUE, ADA)
    # One word is one word: 'Rue'/'R' is not a filing of Rue Quillon, which has one R word.
    assert not could(("Rue", "R"), RUE)
    # A digit is never an initial: 'Local 3' is not a short 'Local 39'.
    assert not could(("Local 3", ""), ("Local 39", ""))


def test_names_link_only_within_a_surname():
    """Per surname, as the rule says. Without it a bare 'Quillon' linked every name with a Q
    initial, and the closure then joined givers across surnames."""
    assert not could(BARE, ("Rue", "Q")), "Q may be Quillon, but they share no surname"
    assert not could(("R Q", ""), RUE), "nothing spelled out in common"
    assert could(("Rue", "Quillon"), RUE), "fields swapped: Rue is the other's surname"
    assert could(("", "Rue"), RUE), "no surname filed: its words could be"
    assert not could(("Quillon", "Rue"), ("Vardle", "Rue")), "a first name is not a surname"


def test_an_initial_is_matched_to_one_word_each():
    """Greedy would pair R with Rue and leave Rue nothing; a matching finds R to Roe."""
    assert could(("Quillon", "R Rue"), ("Quillon", "Rue Roe"))
    assert not could(("Quillon", "R Rue"), ("Quillon", "Rue Mae"))


def test_accents_fold_before_an_initial_is_compared():
    """Unfolded, 'E' was not an initial of 'ÉLISE', and the two read as two givers."""
    assert could(("Quillon", "E"), ("Quillon", "Élise"))
    assert could(("Quillon", "Elise"), ("Quillon", "Élise"))


def test_a_letter_split_off_inside_a_word_is_not_an_initial():
    """'Orrin's' is ORRIN and S. Read as an initial, the S stood for any S word."""
    assert words(("Orrin's", "")) == {"ORRIN", "S"}
    assert words(("QX&T Holdings", "")) == {"QX", "T", "HOLDINGS"}
    assert not could(("Orrin's", ""), ("Orrin", "Sol"))
    assert not could(("QX&T", ""), ("QX", "Tavi"))


def test_only_a_given_name_can_be_an_initial():
    """The last-name field holds a surname, and a one-letter surname is a word. Only where the
    first name is blank and the last-name field holds a whole name can a letter there be one."""
    assert words(("O", "Hanu")) == {"O", "HANU"}
    assert not could(("O", "Hanu"), ("Obrell", "Hanu"))
    assert words(("Quillon, R", "")) == {"QUILLON", "R."}
    # Initials run together are split, and the whole name then sits in the last-name field.
    assert words(("R.M.", "")) == {"R.", "M."}
    assert words(("Quillon", "R.M.")) == {"QUILLON", "R.", "M."}


def test_filing_slips_do_not_split_one_giver():
    """Each read as a different name from the same giver's other filings, so the ranking stood
    with that giver split in two."""
    assert could(("R Quillon", "-"), RUE), "a placeholder first name is no first name"
    assert could(("R.Quillon", ""), RUE), "an initial run into the surname by its point"
    assert could(("Quillon", "R.Mae"), ("Quillon", "Rue Mae"))
    assert could(("Quillon", "O"), ("Quillon", "Øystein")), "a letter NFKD leaves whole"
    assert could(("Quillon", "Æsa"), ("Quillon", "Aesa"))


def test_a_letter_with_no_case_is_a_name_not_an_initial():
    """A one-character given name in a script without case is a name, not an abbreviation."""
    assert not could(("Quillon", "甲"), ("Quillon", "甲乙"))
    assert words(("Quillon", "甲")) == {"QUILLON", "甲"}


# Names over a vocabulary rich in initials, words sharing a first letter, and blanks.
LASTS = ["Quillon", "Q", "", "Quillon Roe", "Vardle"]
FIRSTS = ["", "Rue", "R", "Roe", "Rue M", "R M", "Rue Mae", "M", "Mae", "R Mae", "Ruth", "Élise",
          "E"]
ALL = list(itertools.product(LASTS, FIRSTS))


def linked(a, b):
    """`_could_be` by brute force: no name at all, or a shared spelled-out surname and some
    one-to-one map from the shorter name's words."""
    if not a.words or not b.words:
        return True
    if not any(w in b.words and (w in a.surnames or w in b.surnames) for w in a.words):
        return False
    few, many = sorted((a.words, b.words), key=len)
    return any(all(queries._word_fits(w, t) for w, t in zip(sorted(few), targets))
               for targets in itertools.permutations(sorted(many), len(few)))


def fits(a, b):
    """`_fits` by brute force: some one-to-one map from `a`'s words into `b`'s, every letter
    that has case read as an initial, a word spelled out to itself and an initial to a word
    starting with its letter."""
    def loose(v):
        return sorted(f"{w}." if len(w) == 1 and w.lower() != w else w for w in v.words)

    few, many = loose(a), loose(b)
    return any(all(t[0] == w[0] if queries._initial(w) else t == w for w, t in zip(few, ts))
               for ts in itertools.permutations(many, len(few)))


def components(ws):
    """The groups each name counts in, by brute force: every pair tested, links closed by
    search, and a name in the group of every name it fits. A name with no words is in every
    group, and in one of its own with the other names that have none."""
    keys = [k for k in ws if ws[k].words]
    own = {}
    for k in keys:
        if k in own:
            continue
        own[k], todo = k, [k]
        while todo:
            a = todo.pop()
            for b in keys:
                if b not in own and linked(ws[a], ws[b]):
                    own[b] = k
                    todo.append(b)
    every = frozenset(own.values()) | {"nobody"}
    return {k: frozenset({own[k]} | {own[j] for j in keys if fits(ws[k], ws[j])})
            if ws[k].words else every for k in ws}


def shape(ws, sets):
    """Each name's groups as the names in them, so two groupings compare whatever their ids."""
    return {k: frozenset(frozenset(j for j in ws if g in sets[j]) for g in sets[k]) for k in ws}


def test_the_relation_is_the_one_to_one_map_and_is_symmetric():
    for a, b in itertools.product({queries._name(*n) for n in ALL}, repeat=2):
        assert queries._could_be(a, b) == linked(a, b) == queries._could_be(b, a), (a, b)
        assert queries._fits(a, b) == fits(a, b), (a, b)


def test_every_pair_the_first_late_check_related_is_still_one_giver():
    """#66's late-report check related two names when one's words were all in the other's,
    whatever the surname. Folded into the grouping, it must hold nothing less: a restack that
    folds two guards into one keeps every hold either had."""
    def first_check(a, b):
        wa, wb = ({w for w in re.findall(r"[^\W_]+", f"{f} {l}".upper())} for l, f in (a, b))
        return wa <= wb or wb <= wa

    names = ALL + [("R", ""), ("R M", ""), ("A", ""), ("Quennell", "A"), ("Q", "Rue"),
                   ("Smith", "Rue Q"), ("Odile Marwick", ""), ("Marwick", "Odile")]
    for a, b in itertools.product(names, repeat=2):
        if first_check(a, b):
            assert queries._one_giver(queries._name(*a), queries._name(*b)), (a, b)


def test_the_index_finds_the_same_groups_as_testing_every_pair():
    """Found through an index, never all against all: a pair it missed would leave one giver
    ranked as two."""
    rng = random.Random(129)
    for _ in range(60):
        ws = {i: queries._name(*n) for i, n in enumerate(rng.sample(ALL, rng.randint(2, 18)))}
        assert shape(ws, queries._groups(ws)) == shape(ws, components(ws)), ws


def test_the_index_does_not_test_every_pair(monkeypatch):
    """Tens of thousands of names on a ranking: a first-letter index over every word made each
    initial a candidate for every name sharing the letter."""
    calls = 0

    def counted(real):
        def call(a, b):
            nonlocal calls
            calls += 1
            return real(a, b)
        return call

    monkeypatch.setattr(queries, "_could_be", counted(queries._could_be))
    monkeypatch.setattr(queries, "_fits", counted(queries._fits))
    # Everyone has a middle initial, the same few, and most surnames start with one of them:
    # the shape where indexing a word by its first letter made everyone everyone's candidate.
    ws = {i: queries._name(f"Surname{i:04d}", f"Rue {'SABC'[i % 4]}") for i in range(3000)}
    ws["r"] = queries._name("Surname0007", "R")
    ws["bare"] = queries._name("Surname0009", "")
    groups = queries._groups(ws)
    assert calls < 3100, calls
    assert groups["r"] == groups[7] and groups["bare"] == groups[9] != groups[7]


def test_a_ranking_stands_exactly_when_the_rule_says(tmp_path):
    """Checked against the rule stated plainly, on random rankings: the leader alone in every
    group it counts in, and every other group's bound below the top. A bound is every positive
    gift in the group, and a gift with no readable amount in a group of several names has
    none."""
    rng = random.Random(1290)
    top = 5000.0
    leader = {"nm": "Vardle", "nf": "Tamsin", "amt": top, "n": 1, "kl": "VARDLE",
              "kf": "TAMSIN", "pos": top, "unread": 0}
    pool = [n for n in ALL if n[0] != "Vardle"]
    for _ in range(300):
        rest = []
        for last, first in rng.sample(pool, 9):
            amt = float(rng.randint(1, 30) * 100)
            rest.append({"nm": last, "nf": first, "amt": amt, "n": 1, "kl": last.upper(),
                         "kf": first.upper(), "pos": amt, "unread": int(rng.random() < 0.05)})
        groups = [leader] + sorted(rest, key=lambda g: (-g["amt"], g["kl"], g["kf"]))
        got = queries._could_change_ranking(groups, [leader], top, [], use_names=True)

        ws = {i: queries._name(g["kl"], g["kf"]) for i, g in enumerate(groups)}
        members = {}
        for i, gids in components(ws).items():
            for g in gids:
                members.setdefault(g, []).append(groups[i])
        refuse = False
        for ms in members.values():
            if leader in ms:
                refuse |= len(ms) > 1
            elif len(ms) == 1:
                refuse |= ms[0]["amt"] > top - queries.TOLERANCE
            else:
                bound = (float("inf") if any(g["unread"] for g in ms)
                         else sum(g["pos"] for g in ms))
                refuse |= bound > top - queries.TOLERANCE
        assert bool(got) == refuse, (rest, got)


# --- names with no spelled-out surname in common ----------------------------------------------

# A name that could be a shorter filing of another without a surname in common, and that other.
# #66's late check related each pair (one name's words inside the other's); the surname rule
# alone did not, so these were each held by one guard and not the other.
UNPLACED = [(("R", ""), ("Quillon", "R")), (("R M", ""), ("Quillon", "R M")),
            (("A", ""), ("Quennell", "A")), (("Q", "Rue"), ("Smith", "Rue Q"))]
UNPLACED_IDS = ["initial", "initials", "other-initial", "one-letter-surname"]


def as_asked(name):
    last, first = name
    return {"contributor": last, "contributor_first": first}


def f496(i, name, amount):
    """A Form 496 Part 3 row after the 460's period: pending, and on every schedule."""
    last, first = name
    return (f"{F497}\t0\tF496P3-{i}\t{i}\t{last}\t{first}\t\t\t\t\t9/18/2026{DAY}"
            f"\t{amount}\tF496P3\n")


def build_rows(root, rows, late=()):
    (root / "cache" / "calaccess").mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", (RCPT_HEAD + "".join(rows)).encode("latin-1"))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", (S497_HEAD + "".join(
            late_gift(i, *g) for i, g in enumerate(late, 1))).encode("latin-1"))
    calaccess.build(root)
    return root


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
def test_a_name_with_no_surname_in_common_is_held_in_the_group_it_fits(vague, full):
    """Held in the fuller name's group, never linking it: 'R' could be Quillon's R or anyone
    else's, and that does not make those two one giver."""
    a, b = queries._name(*vague), queries._name(*full)
    assert not queries._could_be(a, b) and queries._fits(a, b) and queries._one_giver(a, b)
    ws = {"vague": a, "full": b, "other": queries._name("Vardle", "R")}
    sets = queries._groups(ws)
    assert sets["full"] <= sets["vague"] and not sets["full"] & sets["other"], sets


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
def test_a_name_with_no_surname_in_common_holds_a_ranking(tmp_path, vague, full):
    """$3,000 and $2,500 that could be one giver's pass a $5,000 leader."""
    root = build(tmp_path, [(VARDLE, "5000"), (vague, "3000"), (full, "2500")])
    got = run(root, "top_contributor")
    assert not got.found and "could be one giver's: up to $5,500" in got.note, got.note
    held = run(root, "top_contributor", names="as_filed")
    assert held.value == "Tamsin Vardle" and held.names and held.unsettled, held.note


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
@pytest.mark.parametrize("form_type", [None, "A", ""], ids=["default", "schedule-A", "every"])
def test_a_name_with_no_surname_in_common_holds_a_total(tmp_path, vague, full, form_type):
    """Either way round: the fuller name's total is held for the vaguer one, and the vaguer
    name's for the fuller one."""
    root = build(tmp_path, [(VARDLE, "5000"), (vague, "300"), (full, "2500")])
    schedule = {} if form_type is None else {"form_type": form_type}
    for asked, other in ((full, vague), (vague, full)):
        got = run(root, "contributor_total", **as_asked(asked), **schedule)
        assert not got.found and _filed(other) in got.note, got.note
        held = run(root, "contributor_total", names="as_filed", **as_asked(asked), **schedule)
        assert held.found and any(n.startswith(_filed(other)) for n in held.names), held.note


def _filed(name):
    return f"{name[0]!r}/{name[1]!r}"


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
def test_a_form_496_row_with_no_surname_in_common_is_held_on_every_schedule(tmp_path, vague,
                                                                           full):
    """#80's every-schedule case, under a name the surname rule alone did not relate: every
    schedule sums the Form 496 Part 3 row under its own name, and the total asked for leaves it
    out."""
    root = build_rows(tmp_path, [gift(1, full, "2500"), f496(2, vague, "9000")])
    every = run(root, "contributor_total", **as_asked(full), form_type="")
    assert not every.found and f"{_filed(vague)} $9,000.00" in every.note, every.note
    held = run(root, "contributor_total", names="as_filed", **as_asked(full), form_type="")
    assert held.value == 2500.0 and held.unsettled, held.note
    # held against the default too, as a late gift no schedule A restates yet
    assert not run(root, "contributor_total", **as_asked(full)).found


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
def test_a_form_497_gift_with_no_surname_in_common_is_held(tmp_path, vague, full):
    """The Form 497 gap: a late gift filed under the vaguer name is held against the fuller
    name's total, and against a ranking it could change."""
    root = build(tmp_path, [(VARDLE, "5000"), (full, "3000")], late=[(vague, "2500")])
    got = run(root, "contributor_total", **as_asked(full))
    assert not got.found and "1 late-report entry ($2,500.00" in got.note, got.note
    named = run(root, "contributor_total", **as_asked(full), form_type="A")
    assert named.value == 3000.0 and [r.amount for r in named.late] == [2500.0], named.note
    ranking = run(root, "top_contributor")
    assert not ranking.found and "late-report" in ranking.note, ranking.note


@pytest.mark.parametrize("vague,full", UNPLACED, ids=UNPLACED_IDS)
def test_late_only_names_with_no_surname_in_common_are_held_as_one(tmp_path, vague, full):
    """Neither name is on schedule A: two $3,000 late gifts that could be one giver's $6,000
    pass the $5,000 leader."""
    root = build(tmp_path, [(VARDLE, "5000")], late=[(vague, "3000"), (full, "3000")])
    got = run(root, "top_contributor")
    assert not got.found and "$0 on schedule-A, $6,000 late)" in got.note, got.note
    assert run(root, "top_contributor", form_type="A").unsettled


# --- top_contributor ---------------------------------------------------------------------


def test_one_giver_filed_two_ways_is_not_ranked_below_a_smaller_one(tmp_path):
    """#114's acceptance case: $3,000 and $2,500 under two filings of one name, behind $5,000.
    """
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (RUE, "2500")])

    got = run(root, "top_contributor")
    assert not got.found and got.value is None, f"ranked as two contributors: {got.value}"
    assert ("names ranked apart that could be one giver's could change the ranking: "
            "'Rue Quillon'/'' $3,000.00 in 1 gift(s) + 'Quillon'/'Rue' $2,500.00 in 1 gift(s), "
            "which could be one giver's: up to $5,500 on schedule-A") in got.note, got.note
    assert got.detail.endswith("names=as_filed ranks each name as filed, for a person to check "
                               "whether they are one giver"), got.note
    assert got.suggestions == ["names=as_filed"]

    a = run(root, "top_contributor", names="as_filed")
    assert a.value == "Tamsin Vardle", a.note
    assert "not counted as one, could change the ranking: 'Rue Quillon'/''" in a.detail
    assert a.names and not a.late, a.unsettled

    assert cite(root, "top_contributor", "Tamsin Vardle").status != "verified"
    # As filed, the ranking goes to a person with the names to check: a note alone was green.
    as_filed = cite(root, "top_contributor", "Tamsin Vardle", names="as_filed")
    assert as_filed.status == "human_review", as_filed.reason
    assert "which could be one giver's: up to $5,500" in as_filed.reason, as_filed.reason


@pytest.mark.parametrize("pair", [
    (RUE, R),                                           # #126: a first initial
    (RUE, RM),                                          # #129: each shorter somewhere
    (("Quillon", "R Mae"), ("Quillon", "Rue M")),        # #129: each spells out the other's
])
def test_a_giver_filed_with_initials_is_not_ranked_below_a_smaller_one(tmp_path, pair):
    root = build(tmp_path, [(VARDLE, "6000"), (pair[0], "3500"), (pair[1], "3000")])

    got = run(root, "top_contributor")
    assert not got.found, f"one giver filed two ways ranked as two: {got.value}"
    assert "which could be one giver's: up to $6,500 on schedule-A" in got.note, got.note


def test_two_different_initials_leave_the_ranking_standing(tmp_path):
    root = build(tmp_path, [(VARDLE, "5000"), (R, "3000"), (S, "3000")])

    got = run(root, "top_contributor")
    assert got.value == "Tamsin Vardle" and got.detail == "$5,000 across 1 schedule-A gift(s)"


def test_a_group_far_below_the_top_changes_nothing(tmp_path):
    root = build(tmp_path, [(VARDLE, "10000"), (WHOLE, "2000"), (RUE, "2000")])

    got = run(root, "top_contributor")
    assert got.value == "Tamsin Vardle" and got.detail == "$10,000 across 1 schedule-A gift(s)"


def test_a_leader_with_another_name_is_not_a_settled_leader(tmp_path):
    """Condition 2: $5 under an initial is in the leader's group. Nothing can pass her from
    outside, but her group could be two givers, and the figure the detail gives her could be
    short. It is flagged whatever its size."""
    root = build(tmp_path, [(RUE, "5000"), (R, "5"), (VARDLE, "3000")])

    got = run(root, "top_contributor")
    assert not got.found, f"a leader in a group with another name stood: {got.value}"
    assert ("'Quillon'/'Rue' $5,000.00 in 1 gift(s) + 'Quillon'/'R' $5.00 in 1 gift(s), which "
            "could be one giver's: up to $5,005") in got.note, got.note
    assert run(root, "top_contributor", names="as_filed").value == "Rue Quillon"


def test_a_tie_never_lists_one_name_twice(tmp_path):
    """#114: two filings of one name, tied, listed as "Rue Quillon | Rue Quillon", read as one
    donor, and together they are no tie at all."""
    root = build(tmp_path, [(WHOLE, "2000"), (RUE, "2000"), (VARDLE, "1000")])

    got = run(root, "top_contributor")
    assert not got.found, f"a tie of one name filed two ways stood: {got.value}"
    assert "which could be one giver's: up to $4,000" in got.note, got.note

    a = run(root, "top_contributor", names="as_filed")
    assert a.value == ("Rue Quillon (filed 'Quillon'/'Rue') | "
                       "Rue Quillon (filed 'Rue Quillon'/'')"), a.value
    assert a.detail.startswith("2-WAY TIE") and "names ranked apart" in a.detail, a.detail


def test_a_bare_surname_joins_every_giver_who_has_it(tmp_path):
    """The closure over-merges on purpose: a bare 'Quillon' links Rue and Ada, so their group's
    bound is all three. That refuses where one giver could not reach the top, and it is the
    cost of a bound that is never low."""
    root = build(tmp_path, [(VARDLE, "4000"), (RUE, "2000"), (ADA, "1500"), (BARE, "500")])

    got = run(root, "top_contributor")
    assert not got.found, got.value
    assert "up to $4,000 on schedule-A" in got.note, got.note
    # Without the bare surname, Rue and Ada are two groups, each well short.
    root = build(tmp_path / "apart", [(VARDLE, "4000"), (RUE, "2000"), (ADA, "1500")])
    assert run(root, "top_contributor").value == "Tamsin Vardle"


def test_an_unnamed_gift_could_be_anyones(tmp_path):
    """No name at all links every name, so no ranking or total stands on the default."""
    root = build(tmp_path, [(VARDLE, "5000"), (NOBODY, "200")])

    assert not run(root, "top_contributor").found
    assert not run(root, "contributor_total", contributor="Vardle",
                   contributor_first="Tamsin").found
    a = run(root, "contributor_total", contributor="Vardle", contributor_first="Tamsin",
            names="as_filed")
    assert a.value == 5000.0 and "''/'' $200.00" in a.detail, a.detail
    assert run(root, "top_contributor", names="as_filed").value == "Tamsin Vardle"


@pytest.mark.parametrize("gifts", [
    [(NOBODY, "5000"), (VARDLE, "3000")],     # a giver with no name leads
    [(NOBODY, "5000")],                       # and nobody else gave
    [(NOBODY, "5000"), (VARDLE, "5000")],     # or ties
])
def test_a_giver_filed_with_no_name_is_never_the_largest(tmp_path, gifts):
    """Its value was "" (or a tie with an empty part), and "" matches "": a green citation
    naming nobody."""
    root = build(tmp_path, gifts)
    for params in ({}, {"names": "as_filed"}, {"form_type": "A", "names": "as_filed"}):
        got = run(root, "top_contributor", **params)
        assert not got.found and got.value is None, (params, got.value)
        assert "filed with no name at all" in got.note, got.note
    assert cite(root, "top_contributor", "", names="as_filed").status != "verified"


def test_a_name_padded_as_python_trims_is_one_name(tmp_path):
    """SQLite trims only ASCII spaces: a surname filed with a non-breaking space was a name of
    its own, displayed exactly like the plain one."""
    root = build(tmp_path, [(("Abbot", "Rue"), "2000"), (("Abbot\xa0", "Rue"), "2750"),
                            (("Aaron", "Rue"), "4750")])
    got = run(root, "top_contributor")
    assert got.value == "Rue Aaron | Rue Abbot", got.note
    assert run(root, "contributor_total", contributor="Abbot",
               contributor_first="Rue").value == 4750.0


def test_a_gifts_two_copies_under_names_that_differ_past_ascii_are_one_gift(tmp_path):
    """The cross-form dedup keyed names with SQLite's ASCII-only UPPER while the queries keyed
    them in Python: schedule A's 'Élise' and Form 496's 'élise' stayed two rows, summed under
    one name, a doubled gift that verified."""
    rows = (f"{F460}\t0\tA-1\t1\tQuillon\tÉlise\t\t\t\t\t3/2/2026{DAY}\t500\tA\n"
            f"{F497}\t0\tF496P3-1\t1\tQuillon\télise\t\t\t\t\t3/2/2026{DAY}\t500\tF496P3\n")
    (tmp_path / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(calaccess.zip_path(tmp_path), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", (RCPT_HEAD + rows).encode("latin-1"))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", S497_HEAD)
    calaccess.build(tmp_path)

    got = run(tmp_path, "contributor_total", contributor="Quillon", contributor_first="Élise",
              form_type="")
    assert got.value == 500.0, got.note
    assert run(tmp_path, "filer_total", form_type="").value == 500.0


def test_a_name_with_no_words_is_in_every_group_and_links_none(tmp_path):
    """Bridged through a '-' filed as a name, every giver was one group, and a total listed an
    unrelated giver as a name that could be the giver's."""
    root = build(tmp_path, [(VARDLE, "5000"), (("Abbot", "Rue"), "900"), (("-", ""), "10")])

    got = run(root, "contributor_total", contributor="Vardle", contributor_first="Tamsin")
    assert not got.found and "'-'/'' $10.00" in got.note, got.note
    assert "Abbot" not in got.note, got.note
    assert not run(root, "top_contributor").found


def test_a_hold_does_not_count_below_zero(tmp_path):
    root = build(tmp_path, [(RUE, "2500"), (R, "3000")], late=[(RUE, "100")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue",
              form_type="A", names="as_filed")
    assert got.late and got.names, got.unsettled
    assert "more late report" not in got.unsettled and "more name" not in got.unsettled


def test_a_late_gift_that_moves_a_ranking_only_with_names_is_held(tmp_path):
    """form_type=A lifts the late gate. Asked alone, the late gift could not reach the top, and
    the ranking went green; with the names it could be one giver with, it could."""
    root = build(tmp_path, [(VARDLE, "5000"), (RUE, "3000"), (R, "1500")],
                 late=[(RM, "600")])

    assert not run(root, "top_contributor").found
    # The names reach $4,500, and with the late gift $5,100. The hold names the late report
    # and the names it links, since the $600 report alone looks too small to matter.
    a = run(root, "top_contributor", form_type="A")
    assert a.value == "Tamsin Vardle" and a.late and a.names, a.unsettled
    assert "'Quillon'/'R' $1,500.00" in a.unsettled, a.unsettled
    assert "they could change the ranking" in a.detail, a.detail
    # Neither gate: only the two together could, so both are held.
    both = run(root, "top_contributor", form_type="A", names="as_filed")
    assert both.value == "Tamsin Vardle" and both.late and both.names, both.unsettled


def test_a_gift_with_no_stated_amount_is_not_a_zero_in_a_group(tmp_path):
    """A blank is money nobody stated. Summed, it was $0, too little to matter, and the group
    could not reach the top."""
    root = build(tmp_path, [(VARDLE, "5000"), (RUE, "1000"), (WHOLE, "")])

    got = run(root, "top_contributor")
    assert not got.found, f"a group with a blank amount could not reach the top? {got.value}"
    # a gift of unknown size could make anyone largest, whatever group it is in
    assert "no largest contributor can be named" in got.note, got.note

    total = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert not total.found, total.note
    assert "'Rue Quillon'/'' 1 gift(s) with no readable amount" in total.note, total.note


def test_a_late_entry_counts_once_in_its_group(tmp_path):
    """A late gift that could be either of two names is one gift. Added to each, a chain summed
    it twice: $2,000 + $500 + $1,400 is $3,900, and twice-counted $5,300 passed $5,000."""
    root = build(tmp_path, [(VARDLE, "5000"), (RUE, "2000"), (R, "500")], late=[(RUE, "1400")])

    got = run(root, "top_contributor")
    assert got.value == "Tamsin Vardle", got.note
    assert "they cannot change the ranking" in got.detail, got.detail
    assert run(root, "top_contributor", names="as_filed").value == "Tamsin Vardle"
    # $100 more, and the group's bound passes the top.
    root = build(tmp_path / "more", [(VARDLE, "5000"), (RUE, "2000"), (R, "500")],
                 late=[(RUE, "2600")])
    got = run(root, "top_contributor")
    assert not got.found and "up to $5,100 on schedule-A, with $2,600 late" in got.note, got.note


def test_a_late_gift_under_an_initial_is_held_against_the_ranking(tmp_path):
    """#126: it matched no ranked name, so it was a giver of its own at $4,000, short."""
    root = build(tmp_path, [(VARDLE, "5000"), (RUE, "2500")], late=[(R, "4000")])

    got = run(root, "top_contributor")
    assert not got.found, f"a late gift under an initial was a giver of its own: {got.value}"
    assert "Rue Quillon ($2,500 on schedule-A, $4,000 late)" in got.note, got.note
    assert "names ranked apart" not in got.note and got.suggestions == ["form_type=A"]


def test_a_late_gift_under_an_accented_name_is_held_against_the_ranking(tmp_path):
    root = build(tmp_path, [(VARDLE, "5000"), (("Quillon", "Élise"), "2500")],
                 late=[(("Quillon", "E"), "4000")])

    got = run(root, "top_contributor")
    assert not got.found and "Élise Quillon ($2,500 on schedule-A, $4,000 late)" in got.note


def test_a_late_giver_filed_two_ways_is_one_giver(tmp_path):
    """No schedule-A row for them, and two late reports, a bare surname and a split name: each
    a giver of their own short of the top, so the ranking stood."""
    root = build(tmp_path, [(VARDLE, "5000")], late=[(BARE, "3000"), (RUE, "2500")])

    got = run(root, "top_contributor")
    assert not got.found, f"a late giver filed two ways was two givers: {got.value}"
    assert "2 late-report entries" in got.note and "names ranked apart" not in got.note
    assert "Quillon ($0 on schedule-A, $5,500 late)" in got.note, got.note
    assert got.detail.endswith("form_type=A ranks schedule A alone, for a person to check "
                               "against these late reports"), got.note


def test_a_late_gift_that_cannot_change_it_is_not_blamed(tmp_path):
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (RUE, "2500")],
                 late=[(("Zedder", "Ada"), "10")])

    got = run(root, "top_contributor")
    assert not got.found and "late-report" not in got.note, got.note
    a = run(root, "top_contributor", form_type="A", names="as_filed")
    assert "not counted — they cannot change the ranking" in a.detail, a.detail
    assert "not counted as one, could change the ranking" in a.detail, a.detail


def test_a_late_gift_and_names_that_only_change_it_together_are_both_named(tmp_path):
    """$3,000 + $1,000 filed another way + $1,500 late passes $5,000; no two of them do."""
    root = build(tmp_path, [(VARDLE, "5000"), (WHOLE, "3000"), (RUE, "1000")],
                 late=[(R, "1500")])

    got = run(root, "top_contributor")
    assert not got.found, got.value
    assert "1 late-report entry" in got.note and "names ranked apart" in got.note, got.note
    assert got.suggestions == ["form_type=A", "names=as_filed"]
    assert run(root, "top_contributor", names="as_filed").found
    assert run(root, "top_contributor", form_type="A").found


def test_a_refusal_reads_the_same_whatever_order_the_rows_come_in(tmp_path):
    rows = [(VARDLE, "5000"), (WHOLE, "3000"), (RUE, "3000"),
            (("Bo Ennis", ""), "3000"), (("Ennis", "Bo"), "3000")]
    notes = {run(build(tmp_path / str(n), order), "top_contributor").note
             for n, order in enumerate((rows, rows[::-1], rows[2:] + rows[:2]))}
    assert len(notes) == 1, notes
    note = notes.pop()
    assert note.index("'Bo Ennis'/''") < note.index("'Rue Quillon'/''"), note


def test_a_name_key_folds_case_and_unicode_form():
    """'José' in any case, and with its accent composed or combining, is one name. Upper-cased
    alone, a combining accent still split it; with SQLite's UPPER, so did a lower-case 'é'."""
    composed, combining = "Jos\u00e9", "Jose\u0301"
    keys = {queries._name_key(n) for n in (composed, combining, "JOS\u00c9", " jos\u00e9\u00a0",
                                           "JOSE\u0301")}
    assert len(keys) == 1, keys
    assert queries._name_key("Rue") != queries._name_key("Roe")


def test_one_name_in_two_cases_past_ascii_is_one_giver(tmp_path):
    """'José' and 'JOSÉ' were two contributors under SQLite's ASCII-only UPPER, each $3,000
    short of the $5,000 leader they pass together."""
    jose = [(("Quillon", "Jos\u00e9"), "3000"), (("QUILLON", "JOS\u00c9"), "3000")]
    root = build(tmp_path, [(VARDLE, "5000"), *jose])

    got = run(root, "top_contributor")
    assert queries._name_key(got.value) == queries._name_key("Jos\u00e9 Quillon"), got.note
    assert got.detail.startswith("$6,000 across 2 schedule-A gift(s)"), got.detail
    total = run(root, "contributor_total", contributor="quillon", contributor_first="jos\u00e9")
    assert total.value == 6000.0, total.note


def test_a_late_gift_restated_under_another_case_is_not_pending(tmp_path):
    """The restatement key reads names as the dedup does: schedule A's 'QUILLON'/'JOSÉ' restates
    the late report's 'Quillon'/'José', so the late gift is not held against anything."""
    rows = (f"{F460}\t0\tA-7\t1\tQUILLON\tJOS\u00c9\t\t\t\t\t10/2/2026{DAY}\t700\tA\n")
    late = (f"{F497}\t0\t1\tS497\tF497P1\tL-7\tIND\tQuillon\tJos\u00e9\t\t\t\t\t11/3/2026{DAY}"
            f"\t10/2/2026{DAY}\t\t700\t\n")
    (tmp_path / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(calaccess.zip_path(tmp_path), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", (RCPT_HEAD + rows).encode("latin-1"))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
        zf.writestr("CalAccess/DATA/S497_CD.TSV", (S497_HEAD + late).encode("latin-1"))
    calaccess.build(tmp_path)

    got = run(tmp_path, "filer_total")
    assert got.value == 700.0 and not got.late, got.note


def test_a_late_givers_name_does_not_depend_on_the_order_rows_come_in(tmp_path):
    """One late gift filed on two reports under one key in two spellings is one entry, and it
    was shown under whichever row was read first. A late giver filed under several names is
    named by all of them, not by whichever entry came first."""
    second = "9991293"
    filings = FILINGS + f"{FILER}\t{second}\tF497\t10/6/2026{DAY}\n"

    def late(filing, last, first, tran="L-7", amount="6000", date="10/2/2026"):
        return (f"{filing}\t0\t1\tS497\tF497P1\t{tran}\tIND\t{last}\t{first}\t\t\t\t"
                f"\t11/3/2026{DAY}\t{date}{DAY}\t\t{amount}\t\n")

    rows = [late(F497, "Quennell", "Ada"), late(second, "QUENNELL", "ADA"),
            late(F497, "Quennell", "Ada B", tran="L-8", amount="100")]
    notes = set()
    for n, order in enumerate((rows, rows[::-1], rows[1:] + rows[:1])):
        root = tmp_path / str(n)
        (root / "cache" / "calaccess").mkdir(parents=True)
        with zipfile.ZipFile(calaccess.zip_path(root), "w") as zf:
            zf.writestr("CalAccess/DATA/RCPT_CD.TSV", RCPT_HEAD + gift(1, VARDLE, "5000"))
            zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", filings)
            zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
            zf.writestr("CalAccess/DATA/S497_CD.TSV", S497_HEAD + "".join(order))
        calaccess.build(root)
        notes.add(run(root, "top_contributor").note)
    assert len(notes) == 1, notes
    note = notes.pop()
    assert "ADA QUENNELL or Ada B Quennell ($0 on schedule-A, $6,100 late)" in note, note


def test_a_refusal_says_where_each_name_in_a_group_gave_from(tmp_path):
    """So a person can tell one giver from two without running another query."""
    root = build(tmp_path, [(VARDLE, "5000"),
                            (WHOLE, "3000", ("Exampleville", "99901", "Example Orchard Co")),
                            (RUE, "2500", ("Otherton", "99902", ""))])

    got = run(root, "top_contributor")
    assert ("'Rue Quillon'/'' $3,000.00 in 1 gift(s) (Exampleville 99901, employer Example "
            "Orchard Co) + 'Quillon'/'Rue' $2,500.00 in 1 gift(s) (Otherton 99902)") in got.note


# --- contributor_total -------------------------------------------------------------------


def test_a_total_asked_for_one_filing_names_the_other(tmp_path):
    """#114's mirror image: asked for Quillon, Rue, it missed the rows filed whole."""
    root = build(tmp_path, [(RUE, "2500"), (WHOLE, "3000"), (VARDLE, "5000")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert not got.found and got.value is None, f"a short total was given: {got.value}"
    assert ("$2,500.00 across 1 itemized schedule-A gift(s), but 1 other name that could be "
            "this giver's, or another giver's ('Rue Quillon'/'' $3,000.00 in 1 gift(s)) — not "
            "a complete total; names=as_filed gives the figure under this name alone, for a "
            "person to check against these names") in got.note, got.note

    a = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue",
            names="as_filed")
    assert a.value == 2500.0, a.note
    assert a.detail.endswith("('Rue Quillon'/'' $3,000.00 in 1 gift(s)), not counted — a figure "
                             "for this name as filed, NOT a complete total; do not word it as "
                             "one"), a.detail

    whole = run(root, "contributor_total", contributor="Rue Quillon")
    assert not whole.found and "'Quillon'/'Rue' $2,500.00" in whole.note, whole.note


@pytest.mark.parametrize("other", [R, RM, ("Quillon", "R Mae"), BARE, WHOLE])
def test_a_total_is_flagged_by_any_other_name_in_its_group(tmp_path, other):
    root = build(tmp_path, [(("Quillon", "Rue M"), "2500"), (other, "20")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue M")
    assert not got.found, f"another name in the group was left out without a word: {got.value}"
    assert run(root, "contributor_total", contributor="Quillon", contributor_first="Rue M",
               names="as_filed").value == 2500.0


def test_a_short_total_under_one_name_does_not_verify_green(tmp_path):
    root = build(tmp_path, [(RUE, "2500"), (R, "3000")])
    who = {"contributor": "Quillon", "contributor_first": "Rue"}

    assert cite(root, "contributor_total", "2500", **who).status != "verified"
    as_filed = cite(root, "contributor_total", "2500", names="as_filed", **who)
    assert as_filed.status == "human_review", as_filed.reason
    assert "it is for names exactly as filed" in as_filed.reason, as_filed.reason
    assert "'Quillon'/'R' $3,000.00" in as_filed.reason, as_filed.reason

    # With no other name in its group, the figure as filed is the whole figure.
    alone = build(tmp_path / "alone", [(RUE, "2500"), (VARDLE, "3000")])
    assert cite(alone, "contributor_total", "2500", names="as_filed",
                **who).status == "verified"


def test_a_value_is_held_exactly_when_the_query_holds_something_against_it(tmp_path):
    """As for late reports: whether a value verifies is decided by what the query held against
    it (`late`, `names`), never by the wording that describes them."""
    root = build(tmp_path, [(VARDLE, "5000"), (RUE, "3000"), (WHOLE, "2500"), (ADA, "100")],
                 late=[(("Zedder", "Ada"), "6000"), (VARDLE, "100")])
    seen = set()
    for name, params in [("top_contributor", {})] + [
            ("contributor_total", {"contributor": last, "contributor_first": first})
            for last, first in (VARDLE, RUE, ADA)]:
        for extra in ({}, {"form_type": "A"}, {"names": "as_filed"},
                      {"form_type": "A", "names": "as_filed"}):
            got = run(root, name, **params, **extra)
            assert bool(got.unsettled) == bool(got.late or got.names), (name, params, extra)
            seen.add((bool(got.late), bool(got.names)))
    assert {(False, False), (True, False), (False, True)} <= seen, seen


def test_a_different_person_sharing_the_surname_is_not_another_name(tmp_path):
    root = build(tmp_path, [(RUE, "2500"), (ADA, "3000"), (S, "900")], late=[(S, "900")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert got.value == 2500.0 and got.detail == "1 itemized schedule-A gift(s)", got.note


def test_a_miss_names_the_name_the_giver_is_filed_under(tmp_path):
    root = build(tmp_path, [(WHOLE, "3000"), (VARDLE, "5000")])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert not got.found and got.rows == 0
    assert "1 other name that could be this giver's, or another giver's ('Rue Quillon'/''" in (
        got.note), got.note


def test_a_late_gift_under_an_initial_is_held_against_the_total(tmp_path):
    """#126: a late entry under 'Quillon'/'R' is held against 'Quillon'/'Rue''s total."""
    root = build(tmp_path, [(RUE, "2500"), (VARDLE, "5000")], late=[(R, "4000")])
    who = {"contributor": "Quillon", "contributor_first": "Rue"}

    got = run(root, "contributor_total", **who)
    assert not got.found and f"1 late-report entry ($4,000.00; filing {F497})" in got.note
    a = run(root, "contributor_total", form_type="A", **who)
    assert a.value == 2500.0 and "NOT a complete total" in a.detail, a.detail


def test_each_gate_has_its_own_way_past(tmp_path):
    """With one switch past both, getting past another name also let a pending late gift
    through. Each switch lifts its own gate."""
    root = build(tmp_path, [(RUE, "2500"), (WHOLE, "3000")], late=[(RUE, "4000")])
    who = {"contributor": "Quillon", "contributor_first": "Rue"}

    names_only = run(root, "contributor_total", names="as_filed", **who)
    assert not names_only.found and "late-report entry" in names_only.note, names_only.note
    assert names_only.suggestions == ["form_type=A"], names_only.suggestions
    late_only = run(root, "contributor_total", form_type="A", **who)
    assert not late_only.found and "other name" in late_only.note, late_only.note
    assert late_only.suggestions == ["names=as_filed"], late_only.suggestions
    both = run(root, "contributor_total", form_type="A", names="as_filed", **who)
    assert both.value == 2500.0, both.note
    assert "late-report entry" in both.detail and "other name" in both.detail, both.detail
    neither = run(root, "contributor_total", **who)
    assert neither.suggestions == ["form_type=A", "names=as_filed"], neither.suggestions

    ranked = build(tmp_path / "rank", [(VARDLE, "5000"), (RUE, "3000"), (WHOLE, "2500")],
                   late=[(("Zedder", "Ada"), "6000")])
    assert run(ranked, "top_contributor", names="as_filed").suggestions == ["form_type=A"]
    assert run(ranked, "top_contributor", form_type="A").suggestions == ["names=as_filed"]


def test_the_name_gate_holds_on_every_schedule(tmp_path):
    root = build(tmp_path, [(RUE, "2500"), (WHOLE, "3000")])
    who = {"contributor": "Quillon", "contributor_first": "Rue"}

    assert not run(root, "contributor_total", form_type="", **who).found
    assert run(root, "contributor_total", form_type="", names="as_filed", **who).value == 2500.0


def test_a_surname_total_is_held_against_the_names_it_counts(tmp_path):
    """Asked for a surname alone, the group is that of the names it counts, not of the
    surname, which every Quillon's name holds."""
    root = build(tmp_path, [(RUE, "2500"), (("Ada Quillon", ""), "20")])
    assert run(root, "contributor_total", contributor="Quillon").value == 2500.0

    root = build(tmp_path / "first", [(RUE, "2500"), (("Rue", ""), "20")])
    got = run(root, "contributor_total", contributor="Quillon")
    assert not got.found and "'Rue'/'' $20.00" in got.note, got.note


def test_a_surname_with_first_names_that_could_be_one_says_so(tmp_path):
    """R and Rue could be one giver's. They were counted as two DIFFERENT first names: the
    refusal was right, and its reason was not."""
    root = build(tmp_path, [(RUE, "2500"), (R, "300")])
    got = run(root, "contributor_total", contributor="Quillon")
    assert not got.found and "DIFFERENT" not in got.note, got.note
    assert ("across 2 first names that could be one giver's or 2 givers' ('R', 'Rue') — not one "
            "contributor as filed; pass contributor_first") in got.note, got.note

    root = build(tmp_path / "two", [(RUE, "2500"), (ADA, "300")])
    got = run(root, "contributor_total", contributor="Quillon")
    assert not got.found and ("across DIFFERENT first names, which cannot all be one giver's "
                              "('Quillon'/'Ada', 'Quillon'/'Rue')") in got.note, got.note


def test_a_bare_surname_does_not_make_two_people_one(tmp_path):
    """Counting people through the closure let a bare-surname late entry link Rue and Tom, and a
    surname-only total for the two of them came back found."""
    root = build(tmp_path, [(RUE, "1000")], late=[(("Quillon", "Tom"), "500"), (BARE, "50")])

    for params in ({}, {"form_type": "A"}, {"names": "as_filed"}):
        got = run(root, "contributor_total", contributor="Quillon", **params)
        assert not got.found and "DIFFERENT first names" in got.note, (params, got.note)


def test_a_total_says_where_each_other_name_gave_from(tmp_path):
    root = build(tmp_path, [(RUE, "2500", ("Exampleville", "99901", "Example Orchard Co")),
                            (WHOLE, "3000", ("Exampleville", "99901", "Example Orchard Co")),
                            (WHOLE, "10", ("Otherton", "99902", "Example Mill"))])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert ("('Rue Quillon'/'' $3,010.00 in 2 gift(s) (Exampleville 99901 / Otherton 99902, "
            "employer Example Mill / Example Orchard Co))") in got.note, got.note


def test_a_database_without_cities_still_names_employers(tmp_path):
    """Built before CTRIB_CITY and CTRIB_ZIP4 were loaded: less to show, nothing refused."""
    head = RCPT_HEAD.replace("\tCTRIB_CITY\tCTRIB_ZIP4", "")
    rows = [(RUE, "2500"), (WHOLE, "3000", ("", "", "Example Mill"))]
    (tmp_path / "cache" / "calaccess").mkdir(parents=True)
    with zipfile.ZipFile(calaccess.zip_path(tmp_path), "w") as zf:
        zf.writestr("CalAccess/DATA/RCPT_CD.TSV", head + "".join(
            gift(i, *g).replace("\t\t\t3/", "\t3/", 1) for i, g in enumerate(rows, 1)))
        zf.writestr("CalAccess/DATA/FILER_FILINGS_CD.TSV", FILINGS)
        zf.writestr("CalAccess/DATA/CVR_CAMPAIGN_DISCLOSURE_CD.TSV", COVERS)
    calaccess.build(tmp_path)

    got = run(tmp_path, "contributor_total", contributor="Quillon", contributor_first="Rue")
    assert "'Rue Quillon'/'' $3,000.00 in 1 gift(s) (employer Example Mill)" in got.note


def test_names_takes_only_as_filed(tmp_path):
    root = build(tmp_path, [(RUE, "2500")])
    for bad in ("yes", "AS_FILED", " as_filed"):
        with pytest.raises(ValueError, match="names must be 'as_filed'"):
            run(root, "top_contributor", names=bad)
        with pytest.raises(ValueError, match="names must be 'as_filed'"):
            run(root, "contributor_total", contributor="Quillon", names=bad)


def test_more_other_names_than_one_lookup_holds_are_all_named(tmp_path):
    """The other names are summed in chunks, since each is two SQL variables."""
    middles = [(f"Rue X{i:03d} Quillon", "") for i in range(600)]
    root = build(tmp_path, [(RUE, "2500")] + [(m, "10") for m in middles])

    got = run(root, "contributor_total", contributor="Quillon", contributor_first="Rue",
              names="as_filed")
    assert got.value == 2500.0
    assert "600 other names that could be this giver's" in got.detail, got.detail


def test_the_cli_prints_a_flag_as_filed_text_not_markup(tmp_path):
    """An employer is filer text, and the flag reaches the console: rich read '[bold]' as a
    style and dropped it."""
    from typer.testing import CliRunner

    from vgpipe import cli

    root = build(tmp_path, [(RUE, "2500"), (WHOLE, "3000", ("", "", "[bold]Mill[/bold]"))])
    r = CliRunner().invoke(cli.app, ["query", "calaccess.contributor_total", "--cache", str(root),
                                     "--param", f"filer_id={FILER}", "--param",
                                     "contributor=Quillon", "--param", "contributor_first=Rue"])
    assert r.exit_code == 1, r.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", " ".join(r.output.split()))
    assert "employer [bold]Mill[/bold]" in plain, plain
