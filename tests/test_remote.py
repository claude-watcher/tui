"""Sessions distantes : résolution, adaptation, garde-fous, affichage.

Fonctions pures et helpers d'affichage — aucune UI. Deux tests (redirections non
suivies, ligne de statut hostile) ouvrent un serveur HTTP sur la BOUCLE LOCALE :
c'est le seul moyen de prouver ce que fait vraiment urllib, et ça ne sort pas de
la machine.
"""

import json
import stat as stat_mod
import sys
import threading
import time

import pytest

# ── Résolution des remotes (config + drapeaux + tokens) ──────────────────────

def test_flag_alone_declares_a_remote(watcher):
    remotes = watcher.resolve_remotes({}, [('lab', 'https://box:8000')], env={})
    assert len(remotes) == 1
    assert remotes[0]['name'] == 'lab'
    assert remotes[0]['url'] == 'https://box:8000'
    assert remotes[0]['label'] == 'lab'
    assert remotes[0]['token'] is None
    assert remotes[0]['enabled'] is True


def test_flag_url_wins_but_section_keys_survive(watcher):
    sections = {'lab': {'url': 'https://old:8000', 'token': 'sekrit', 'label': 'Labo'}}
    remotes = watcher.resolve_remotes(sections, [('lab', 'https://new:9000')], env={})
    assert len(remotes) == 1
    assert remotes[0]['url'] == 'https://new:9000'   # le drapeau nomme, il n'efface pas
    assert remotes[0]['token'] == 'sekrit'
    assert remotes[0]['label'] == 'Labo'


def test_token_from_url_beats_env_and_section(watcher):
    sections = {'lab': {'url': 'https://box/', 'token': 'from-section'}}
    remotes = watcher.resolve_remotes(
        sections, [('lab', 'https://remote:from-url@box:8000/')],
        env={'CW_REMOTE_TOKEN_LAB': 'from-env'})
    assert remotes[0]['token'] == 'from-url'
    assert remotes[0]['url'] == 'https://box:8000/'   # userinfo retiré


def test_token_from_env_beats_section(watcher):
    sections = {'lab': {'url': 'https://box/', 'token': 'from-section'}}
    remotes = watcher.resolve_remotes(sections, [], env={'CW_REMOTE_TOKEN_LAB': 'from-env'})
    assert remotes[0]['token'] == 'from-env'


def test_token_from_section_when_nothing_else(watcher):
    sections = {'lab': {'url': 'https://box/', 'token': 'from-section'}}
    assert watcher.resolve_remotes(sections, [], env={})[0]['token'] == 'from-section'


def test_no_token_anywhere(watcher):
    assert watcher.resolve_remotes({'lab': {'url': 'https://box/'}}, [], env={})[0]['token'] is None


def test_token_env_var_name_normalises_the_remote_name(watcher):
    assert watcher.remote_token_env('my-lab.2') == 'CW_REMOTE_TOKEN_MY_LAB_2'


def test_env_token_uses_the_normalised_name(watcher):
    remotes = watcher.resolve_remotes({}, [('my-lab', 'https://box/')],
                                      env={'CW_REMOTE_TOKEN_MY_LAB': 'tok'})
    assert remotes[0]['token'] == 'tok'


@pytest.mark.parametrize('a, b', [('a-b', 'a.b'), ('a-b', 'a_b'), ('lab', 'LAB')])
def test_colliding_token_env_vars_are_refused(watcher, a, b):
    """`a-b` et `a.b` retombent sur CW_REMOTE_TOKEN_A_B : sans détection, le token
    d'un hôte de confiance part vers un hôte sans rapport."""
    sections = {a: {'url': 'https://one/'}, b: {'url': 'https://two/'}}
    with pytest.raises(ValueError) as e:
        watcher.resolve_remotes(sections, [], env={watcher.remote_token_env(a): 'tok'})
    assert watcher.remote_token_env(a) in str(e.value)


@pytest.mark.parametrize('a, b', [('a-b', 'a.b'), ('lab', 'LAB')])
def test_colliding_names_without_the_env_var_still_start(watcher, a, b):
    """Sans la variable, aucun secret ne peut partir au mauvais endroit.

    Refuser de démarrer pour une collision purement théorique bloquait
    l'utilisateur sans rien protéger — et le refus remonte jusqu'à un SystemExit.
    """
    sections = {a: {'url': 'https://one/'}, b: {'url': 'https://two/'}}
    assert len(watcher.resolve_remotes(sections, [], env={})) == 2


def test_distinct_names_do_not_trip_the_collision_check(watcher):
    sections = {'lab': {'url': 'https://one/'}, 'prod': {'url': 'https://two/'}}
    assert len(watcher.resolve_remotes(sections, [], env={})) == 2


def test_a_url_less_section_cannot_trip_the_collision_check(watcher):
    """Une section sans url est IGNORÉE : refuser de démarrer à cause d'elle
    serait un faux positif."""
    sections = {'a-b': {'url': 'https://one/'}, 'a.b': {'token': 'x'}}
    assert [r['name'] for r in watcher.resolve_remotes(sections, [], env={})] == ['a-b']


@pytest.mark.parametrize('value, expected', [
    ('1', True), ('yes', True), ('true', True), ('on', True), ('TRUE', True), (' On ', True),
    ('0', False), ('no', False), ('false', False), ('off', False), ('OFF', False),
])
def test_enabled_accepts_the_full_boolean_truth_table(watcher, value, expected):
    """Seul le littéral « false » désactivait : « no » / « 0 » / « off » laissaient
    le token partir vers un hôte que l'utilisateur croyait éteint."""
    remotes = watcher.resolve_remotes({'lab': {'url': 'https://box/', 'enabled': value}}, [], env={})
    assert remotes[0]['enabled'] is expected


def test_unparseable_enabled_is_refused_not_defaulted_to_on(watcher):
    with pytest.raises(ValueError) as e:
        watcher.resolve_remotes({'lab': {'url': 'https://box/', 'enabled': 'maybe'}}, [], env={})
    assert '[remote:lab]' in str(e.value) and 'maybe' in str(e.value)


def test_disabled_section_is_kept_but_flagged(watcher):
    remotes = watcher.resolve_remotes({'lab': {'url': 'https://box/', 'enabled': 'false'}}, [], env={})
    assert remotes[0]['enabled'] is False


def test_the_enabled_filter_lives_in_exactly_one_place(watcher):
    """resolve_remotes rend TOUT (l'écran de paramètres doit voir les éteints) ;
    enabled_remotes est le seul filtre."""
    remotes = watcher.resolve_remotes(
        {'on': {'url': 'https://a/'}, 'off': {'url': 'https://b/', 'enabled': 'no'}}, [], env={})
    assert {r['name'] for r in remotes} == {'on', 'off'}
    assert [r['name'] for r in watcher.enabled_remotes(remotes)] == ['on']


def test_flag_reenables_a_disabled_section(watcher):
    remotes = watcher.resolve_remotes({'lab': {'url': 'https://box/', 'enabled': 'false'}},
                                      [('lab', 'https://box2/')], env={})
    assert remotes[0]['enabled'] is True


def test_section_without_url_is_dropped(watcher):
    assert watcher.resolve_remotes({'lab': {'token': 'x'}}, [], env={}) == []


def test_two_remotes_sharing_a_hostname_stay_distinct(watcher):
    remotes = watcher.resolve_remotes(
        {}, [('a', 'https://box:8000'), ('b', 'https://box:9000')], env={})
    assert [r['label'] for r in remotes] == ['a', 'b']


def test_long_label_is_elided(watcher):
    remotes = watcher.resolve_remotes(
        {'x': {'url': 'https://box/', 'label': 'a-very-long-machine-name'}}, [], env={})
    assert remotes[0]['label'] == 'a-very-long…'
    assert len(remotes[0]['label']) == watcher.REMOTE_LABEL_MAX


def test_remote_flag_shape_is_validated(watcher):
    import argparse
    assert watcher.parse_remote_flag('lab=https://box') == ('lab', 'https://box')
    for bad in ('lab', '=https://box', 'lab='):
        with pytest.raises(argparse.ArgumentTypeError):
            watcher.parse_remote_flag(bad)


def test_the_hash_token_form_is_refused_with_the_supported_ones_named(watcher):
    """`NAME=URL#TOKEN` vient d'un brouillon abandonné : le fragment mangeait le
    chemin, aucun en-tête d'auth ne partait, et le secret finissait non rédigé."""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError) as e:
        watcher.parse_remote_flag('lab=https://box:8000/#s3cr3t')
    msg = str(e.value)
    assert 'CW_REMOTE_TOKEN_NAME' in msg and 'remote:TOKEN@' in msg


# ── URL : userinfo, token, rédaction ─────────────────────────────────────────

def test_userinfo_is_stripped_and_token_is_the_password(watcher):
    clean, token, shown = watcher.split_remote_url('https://remote:s3cr3t@box:8000/')
    assert clean == 'https://box:8000/'
    assert token == 's3cr3t'
    assert 's3cr3t' not in shown


def test_urllib_would_not_handle_the_userinfo_itself(watcher):
    """La raison d'être de split_remote_url : urllib laisse le userinfo dans l'hôte."""
    import urllib.request
    naive = urllib.request.Request('https://remote:s3cr3t@box:8000/api/sessions')
    assert naive.host == 'remote:s3cr3t@box:8000'     # DNS sur cette chaîne → échec
    assert not naive.headers                          # aucun en-tête d'auth ajouté
    clean, token, _ = watcher.split_remote_url('https://remote:s3cr3t@box:8000/api/sessions')
    assert urllib.request.Request(clean).host == 'box:8000'
    assert token == 's3cr3t'


def test_userinfo_without_colon_is_the_token(watcher):
    clean, token, shown = watcher.split_remote_url('https://s3cr3t@box/')
    assert (clean, token) == ('https://box/', 's3cr3t')
    assert shown == 'https://***@box/'


def test_an_empty_password_falls_back_to_the_username_like_the_server(watcher):
    """`https://tok:@hote/` : le serveur retient « tok » (mot de passe vide → nom
    d'utilisateur). Le client rendait None — les deux bouts divergeaient, ce que
    la docstring de split_remote_url prétend impossible."""
    assert watcher.split_remote_url('https://tok:@box/')[1] == 'tok'


def test_url_without_userinfo_is_untouched(watcher):
    assert watcher.split_remote_url('https://box:8000/') == (
        'https://box:8000/', None, 'https://box:8000/')


def test_redacted_url_keeps_the_username_and_hides_the_password(watcher):
    assert watcher.split_remote_url('https://remote:s3cr3t@box:8000/')[2] == \
        'https://remote:***@box:8000/'


def test_redaction_reaches_every_display_path(watcher):
    """display_url est la SEULE URL exposée : elle ne doit jamais porter le token."""
    r = watcher.resolve_remotes({}, [('lab', 'http://remote:hunter2@box:8000/')], env={})[0]
    assert 'hunter2' not in r['display_url']
    assert r['display_url'] == 'http://remote:***@box:8000/'


@pytest.mark.parametrize('url', [
    'https://box:8000/?key=s3cr3t',
    'https://box:8000/?foo=1&key=s3cr3t',
    'https://remote:pwd@box:8000/?key=s3cr3t',
])
def test_a_key_query_token_is_never_shown_verbatim(watcher, url):
    """Le webui refuse désormais un token en query — mais l'URL qu'on nous donne
    peut encore en porter un (habitude, doc plus ancienne), et il ne doit alors
    paraître ni dans l'infobulle ni dans --once."""
    r = watcher.resolve_remotes({}, [('lab', url)], env={})[0]
    assert 's3cr3t' not in r['display_url']
    assert 'key=***' in r['display_url']
    # …mais l'URL réellement interrogée garde la query intacte : le client ne
    # réécrit pas l'URL qu'on lui a donnée. Elle n'authentifie plus rien côté
    # webui — c'est l'en-tête X-API-Key qui le fait — et un reverse proxy devant
    # lui peut avoir besoin de ses propres paramètres.
    assert 'key=s3cr3t' in r['url']


def test_masking_a_query_keeps_the_parameter_names(watcher):
    assert watcher.mask_query('key=s3cr3t&debug=1') == 'key=***&debug=***'
    assert watcher.mask_query('') == ''
    assert watcher.mask_query('flag') == 'flag'


def test_ipv6_host_survives_the_split(watcher):
    clean, token, _ = watcher.split_remote_url('http://remote:tok@[::1]:8000/')
    assert (clean, token) == ('http://[::1]:8000/', 'tok')


# ── Adaptateur : mapping des états ───────────────────────────────────────────

REMOTE = {'name': 'lab', 'label': 'lab'}


def _row(**kw):
    base = {'pid': 42, 'project': 'proj', 'cwd': '/home/u/proj', 'display_cwd': '/home/u/proj',
            'state': 'idle', 'idle_seconds': 10, 'elapsed': 300, 'daemon': False}
    base.update(kw)
    return base


@pytest.mark.parametrize('state, expected', [
    ('waiting',    {'waiting': True,  'working': False, 'background': False}),
    ('working',    {'waiting': False, 'working': True,  'background': False}),
    ('background', {'waiting': False, 'working': False, 'background': True}),
    ('idle',       {'waiting': False, 'working': False, 'background': False}),
])
def test_state_maps_to_booleans(watcher, state, expected):
    s = watcher.adapt_remote_row(_row(state=state), REMOTE, 1000.0)
    for k, v in expected.items():
        assert s[k] is v


def test_daemon_row_is_marked_and_never_active(watcher):
    s = watcher.adapt_remote_row(_row(state='daemon', daemon=True, idle_seconds=None),
                                 REMOTE, 1000.0)
    assert s['daemon'] is True
    assert (s['waiting'], s['working'], s['background']) == (False, False, False)
    assert s['last_activity'] is None


def test_unknown_state_degrades_to_idle(watcher):
    s = watcher.adapt_remote_row(_row(state='teleporting'), REMOTE, 1000.0)
    assert (s['waiting'], s['working'], s['background']) == (False, False, False)


def test_remote_row_carries_the_caller_supplied_label(watcher):
    s = watcher.adapt_remote_row(_row(), {'name': 'lab', 'label': 'Labo'}, 1000.0)
    assert s['remote'] == 'Labo' and s['remote_name'] == 'lab'


def test_local_action_fields_are_forced_to_none(watcher):
    s = watcher.adapt_remote_row(
        _row(window_id='0xdead', terminal_pid=1234, kitty_socket='/tmp/s',
             kitty_window_id='7'), REMOTE, 1000.0)
    for k in ('window_id', 'terminal_pid', 'kitty_socket', 'kitty_window_id'):
        assert s[k] is None


# ── Adaptateur : horloges ────────────────────────────────────────────────────

def test_idle_is_skew_proof(watcher):
    """On n'importe qu'une DURÉE : l'instant de référence est LOCAL, donc l'écart
    d'horloge murale entre les deux machines n'entre nulle part.

    Construit à partir de deux réceptions locales séparées d'une heure — la
    seconde moitié était auparavant `f(x) == f(x)`, qui ne prouve rien.
    """
    here = watcher.adapt_remote_row(_row(idle_seconds=120), REMOTE, 1_000_000.0)
    an_hour_later = watcher.adapt_remote_row(_row(idle_seconds=120), REMOTE, 1_003_600.0)
    # Chaque machine calcule time.time() - last_activity avec SON horloge : 120 s.
    assert 1_000_000.0 - here['last_activity'] == pytest.approx(120)
    assert 1_003_600.0 - an_hour_later['last_activity'] == pytest.approx(120)
    # Et l'ancrage suit l'instant de réception, pas une horloge distante.
    assert an_hour_later['last_activity'] - here['last_activity'] == pytest.approx(3600)


def test_an_absolute_remote_timestamp_is_never_imported(watcher):
    """Un `last_activity` absolu dans le payload (horloge de l'autre machine,
    en avance d'une heure) doit être IGNORÉ au profit de la durée."""
    s = watcher.adapt_remote_row(_row(idle_seconds=120, last_activity=1_003_600.0),
                                 REMOTE, 1_000_000.0)
    assert s['last_activity'] == pytest.approx(999_880.0)


def test_snapshot_age_is_subtracted_from_last_activity(watcher):
    rows, _ = watcher.adapt_remote_payload(
        {'sessions': [_row(idle_seconds=100)], 'age_seconds': 30.0}, REMOTE, 1000.0)
    assert rows[0]['last_activity'] == pytest.approx(1000.0 - 100 - 30)


def test_missing_age_seconds_is_treated_as_zero(watcher):
    """webui antérieur au cache : pas d'age_seconds, le remote marche quand même."""
    rows, _ = watcher.adapt_remote_payload({'sessions': [_row(idle_seconds=100)]}, REMOTE, 1000.0)
    assert rows[0]['last_activity'] == pytest.approx(900.0)


def test_hostile_age_seconds_does_not_move_time_forward(watcher):
    rows, _ = watcher.adapt_remote_payload(
        {'sessions': [_row(idle_seconds=10)], 'age_seconds': -99999}, REMOTE, 1000.0)
    assert rows[0]['last_activity'] == pytest.approx(990.0)


def test_negative_idle_is_clamped(watcher):
    s = watcher.adapt_remote_row(_row(idle_seconds=-500), REMOTE, 1000.0)
    assert s['last_activity'] == pytest.approx(1000.0)


# ── Adaptateur : flottants non finis (gel du client) ─────────────────────────

@pytest.mark.parametrize('literal', ['Infinity', '-Infinity', 'NaN'])
@pytest.mark.parametrize('field', ['idle_seconds', 'elapsed'])
def test_non_finite_numbers_never_reach_a_row(watcher, literal, field):
    """json.dumps ÉMET `Infinity` par défaut et json.loads l'accepte : un webui
    simplement buggé suffisait à faire lever OverflowError dans format_idle, EN
    PLEIN RENDU. Le rejet est au point d'étranglement (_as_float)."""
    payload = json.loads(
        f'{{"sessions": [{{"pid": 42, "{field}": {literal}}}], "age_seconds": 0.0}}')
    rows, _ = watcher.adapt_remote_payload(payload, REMOTE, 1000.0)
    assert len(rows) == 1
    la = rows[0]['last_activity']
    assert la is None or (la == la and abs(la) != float('inf'))
    # Et la valeur passe le rendu sans lever.
    watcher.format_elapsed(rows[0]['elapsed'])
    if la is not None:
        watcher.format_idle(1000.0 - la, 'precise')


def test_a_non_finite_snapshot_age_is_ignored(watcher):
    payload = json.loads('{"sessions": [{"pid": 42, "idle_seconds": 10}], '
                         '"age_seconds": Infinity}')
    rows, _ = watcher.adapt_remote_payload(payload, REMOTE, 1000.0)
    assert rows[0]['last_activity'] == pytest.approx(990.0)


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan'), 10 ** 400])
def test_as_float_rejects_every_non_finite_value(watcher, value):
    assert watcher._as_float(value) is None


def test_as_float_still_accepts_ordinary_numbers(watcher):
    assert watcher._as_float(3) == 3.0
    assert watcher._as_float(-2.5) == -2.5
    assert watcher._as_float(True) is None      # un booléen n'est pas une durée
    assert watcher._as_float('42') is None


def test_elapsed_is_clamped_to_a_ceiling(watcher):
    """`9223372036854775808` s'affichait « 2562047788015215h30m »."""
    s = watcher.adapt_remote_row(_row(elapsed=2 ** 63), REMOTE, 1000.0)
    assert s['elapsed'] == watcher.REMOTE_MAX_ELAPSED_S
    assert len(watcher.format_elapsed(s['elapsed'])) <= 12


def test_age_seconds_is_clamped_like_idle(watcher):
    """Le terme voisin de la MÊME soustraction, laissé non borné au round 1.

    `_as_float` ne rejette que les non-finis : 1e308 est fini et passait, donc
    l'enveloppe rouvrait par `age_seconds` la cellule de 311 caractères que le
    plafond sur `idle` venait de fermer.
    """
    rows, _ = watcher.adapt_remote_payload(
        {'sessions': [_row(idle_seconds=10)], 'age_seconds': 1e308},
        REMOTE, 1_000_000.0)
    idle = 1_000_000.0 - rows[0]['last_activity']
    assert idle <= watcher.REMOTE_MAX_ELAPSED_S
    assert len(watcher.format_idle(idle, 'precise')) <= 16


def test_idle_is_clamped_to_the_same_ceiling(watcher):
    """`_as_float` rejette les non-finis, mais 1e308 est FINI et passait.

    Même classe que `elapsed`, un champ plus loin : la durée d'inactivité
    produisait une cellule de 311 caractères dans une colonne dimensionnée sur
    son contenu.
    """
    s = watcher.adapt_remote_row(_row(idle_seconds=1e308), REMOTE, 1000.0)
    idle = 1000.0 - s['last_activity']
    assert idle <= watcher.REMOTE_MAX_ELAPSED_S
    assert len(watcher.format_idle(idle, 'precise')) <= 16


# ── Adaptateur : payload hostile / malformé ──────────────────────────────────

def test_bad_rows_are_dropped_and_good_ones_survive(watcher):
    payload = {'sessions': [
        {'pid': 'not-an-int', 'project': 'x'},        # pid non entier → jetée
        {'pid': None},                                # pid nul → jetée
        'not-a-dict',                                 # pas un dict → jetée
        {'pid': True},                                # bool n'est pas un pid
        _row(pid=7, idle_seconds='42'),               # idle mal typé → gardée, sans idle
        _row(pid=8),
    ]}
    rows, total = watcher.adapt_remote_payload(payload, REMOTE, 1000.0)
    assert [r['pid'] for r in rows] == [7, 8]
    assert rows[0]['last_activity'] is None           # '42' rejeté, pas d'exception
    assert total == 6                                 # total ANNONCÉ, pas total gardé


@pytest.mark.parametrize('payload', [
    None, [], 'nope', 42, {}, {'sessions': None}, {'sessions': 'x'}, {'sessions': {}},
])
def test_shapeless_payloads_yield_no_rows_and_no_exception(watcher, payload):
    assert watcher.adapt_remote_payload(payload, REMOTE, 1000.0) == ([], 0)


def test_wrong_typed_scalars_degrade_field_by_field(watcher):
    s = watcher.adapt_remote_row(
        _row(project=None, cwd=42, elapsed='300', context_pct='80', agents='nope',
             topic=[1, 2], tool={}), REMOTE, 1000.0)
    assert s['project'] == '?' and s['cwd'] == '?' and s['elapsed'] == 0
    assert s['context_pct'] is None and s['agents'] == [] and s['topic'] is None
    assert s['tool'] is None


def test_context_pct_is_clamped(watcher):
    assert watcher.adapt_remote_row(_row(context_pct=999), REMOTE, 1000.0)['context_pct'] == 100
    assert watcher.adapt_remote_row(_row(context_pct=-5), REMOTE, 1000.0)['context_pct'] == 0


def test_row_flood_is_capped_and_the_truncation_is_reported(watcher):
    """Tronquer en silence donne un tableau qui a l'air complet."""
    n = watcher.REMOTE_MAX_ROWS + 50
    rows, total = watcher.adapt_remote_payload(
        {'sessions': [_row(pid=i) for i in range(n)]}, REMOTE, 1000.0)
    assert len(rows) == watcher.REMOTE_MAX_ROWS
    assert total == n


def test_malformed_agents_are_filtered(watcher):
    s = watcher.adapt_remote_row(
        _row(agents=['x', {'no_name': 1}, {'name': 'ok', 'type': 't', 'model': 'm'}]),
        REMOTE, 1000.0)
    assert s['agents'] == [{'pid': None, 'name': 'ok', 'type': 't', 'model': 'm'}]
    assert watcher.adapt_remote_agents('nope') == []


# ── Adaptateur : chaînes venues du réseau (frontière de confiance) ───────────

def test_ansi_and_control_chars_are_stripped(watcher):
    assert watcher.clean_remote_str('\x1b[2Jclear\r\nme\x00') == 'clearme'
    assert watcher.clean_remote_str('\x1b]0;title\x07x') == 'x'
    assert watcher.clean_remote_str('\x1b[38;5;196mred') == 'red'


@pytest.mark.parametrize('ch', ['؜', '​', '‎', '‏', ' ',
                                ' ', '‮', '⁦', '⁩', '﻿'])
def test_unicode_format_controls_are_stripped(watcher, ch):
    """U+202E & voisins RÉORDONNENT visuellement une chaîne — c'est la primitive
    d'usurpation de label que la spec nomme ; U+2028/2029 sont des sauts de ligne
    pour tout moteur de rendu et casseraient « une ligne = une ligne »."""
    assert watcher.clean_remote_str(f'a{ch}b') == 'ab'


def test_an_rlo_reversed_label_cannot_spoof_another_remote(watcher):
    spoof = 'gpj.‮gnp.evil'            # rendu à l'envers dans un terminal
    s = watcher.adapt_remote_row(_row(project=spoof), REMOTE, 1000.0)
    assert '‮' not in s['project']


def test_strings_are_length_capped(watcher):
    assert len(watcher.clean_remote_str('a' * 2000, 200)) == 200


def test_escape_sequences_never_reach_a_rendered_field(watcher):
    s = watcher.adapt_remote_row(
        _row(project='\x1b[2Jproj', topic='\rtop\nic', cwd='/tmp/\x1b[31mx',
             worktree='wt\x1b[0m', tool='\x07bash',
             agents=[{'name': '\x1b[2Jbob', 'type': 'x\r', 'model': 'm\n'}]),
        REMOTE, 1000.0)
    for value in (s['project'], s['topic'], s['cwd'], s['worktree'], s['tool'],
                  s['agents'][0]['name'], s['agents'][0]['type'], s['agents'][0]['model']):
        assert '\x1b' not in value and '\r' not in value and '\n' not in value
    assert s['project'] == 'proj' and s['agents'][0]['name'] == 'bob'


def test_a_remote_cannot_spoof_another_remotes_label(watcher):
    """Le label vient du CLIENT, jamais du payload."""
    s = watcher.adapt_remote_row(_row(remote='prod', remote_name='prod'),
                                 {'name': 'lab', 'label': 'lab'}, 1000.0)
    assert s['remote'] == 'lab' and s['remote_name'] == 'lab'


def test_a_2000_char_topic_cannot_blow_up_a_row(watcher):
    s = watcher.adapt_remote_row(_row(topic='x' * 2000), REMOTE, 1000.0)
    assert len(s['topic']) == 400


# ── Sécurité : kill / focus au point d'étranglement ──────────────────────────

def test_kill_session_refuses_a_remote_row(watcher, monkeypatch):
    """Appel DIRECT : la garde est dans la fonction, pas dans le gestionnaire de clic.
    Un pid distant 1234 désigne un process LOCAL sans rapport."""
    killed = []
    monkeypatch.setattr(watcher.os, 'kill', lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(watcher, 'get_session_registry', lambda *a, **k: {'ok': True})
    remote_row = watcher.adapt_remote_row(_row(pid=1234), REMOTE, 1000.0)
    assert watcher.kill_session(remote_row) is False
    assert killed == []
    # Contrôle : la même ligne SANS le marqueur distant est bien tuée — sinon le
    # test passerait aussi avec un kill_session cassé.
    local_row = dict(remote_row, remote=None)
    assert watcher.kill_session(local_row) is True
    assert killed == [1234]


def test_focus_terminal_refuses_a_remote_row(watcher, monkeypatch):
    calls = []
    monkeypatch.setattr(watcher.subprocess, 'run',
                        lambda *a, **k: calls.append(a) or type('R', (), {'returncode': 0})())
    remote_row = dict(watcher.adapt_remote_row(_row(), REMOTE, 1000.0),
                      window_id='0xdead', terminal_pid=99)
    assert watcher.focus_terminal(remote_row) is False
    assert calls == []
    assert watcher.focus_terminal(dict(remote_row, remote=None)) is True
    assert calls != []


def test_remote_rows_are_excluded_from_local_inotify_watches(watcher):
    """config_dir d'une ligne distante ne doit JAMAIS servir de chemin local :
    le ~/.claude d'un remote existe aussi ici. Assertion sur la FONCTION du
    script, pas sur une reformulation de sa logique dans le test."""
    rows = [watcher.adapt_remote_row(_row(config_dir='/home/u/.claude'), REMOTE, 1000.0),
            {'pid': 1, 'config_dir': '/home/u/.claude-local'}]
    assert watcher.local_config_dirs(rows) == ['/home/u/.claude-local']


def test_local_config_dirs_skips_rows_without_a_config_dir(watcher):
    assert watcher.local_config_dirs([{'pid': 1}, {'pid': 2, 'config_dir': None}]) == []


# ── Fusion et tri ────────────────────────────────────────────────────────────

def test_sorting_ignores_the_label(watcher, monkeypatch):
    """Une session en attente sur lab doit voisiner une session en attente locale."""
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    remote_waiting = watcher.adapt_remote_row(
        _row(pid=1, project='zzz', state='waiting'), REMOTE, 1000.0)
    local_idle = dict(remote_waiting, pid=2, project='aaa', remote=None, waiting=False)
    remote_idle = watcher.adapt_remote_row(_row(pid=3, project='bbb'), REMOTE, 1000.0)
    out = watcher.scan_sessions([local_idle, remote_idle, remote_waiting])
    assert [s['pid'] for s in out] == [1, 2, 3]   # waiting d'abord, puis alpha


def test_idle_sort_mode_orders_the_idle_group_by_recency(watcher, monkeypatch):
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'idle', raising=False)
    now = time.time()
    old = watcher.adapt_remote_row(_row(pid=1, project='aaa', idle_seconds=9000), REMOTE, now)
    fresh = watcher.adapt_remote_row(_row(pid=2, project='zzz', idle_seconds=1), REMOTE, now)
    assert [s['pid'] for s in watcher.scan_sessions([old, fresh])] == [2, 1]


def test_no_local_skips_the_proc_scan(watcher, monkeypatch):
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher, 'scan_local_sessions',
                        lambda: pytest.fail('scan local exécuté sous --no-local'))
    assert watcher.scan_sessions([]) == []


def test_local_scan_runs_by_default(watcher, monkeypatch):
    monkeypatch.setattr(watcher.CFG, 'no_local', False, raising=False)
    monkeypatch.setattr(watcher, 'scan_local_sessions',
                        lambda: [dict(_row(pid=5), waiting=False, working=False,
                                      background=False, project='local')])
    assert [s['pid'] for s in watcher.scan_sessions(None)] == [5]


def test_hide_daemons_applies_to_remote_rows_too(watcher, monkeypatch):
    """Filtré dans le seul scan local, l'option laissait passer les démons distants."""
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    monkeypatch.setattr(watcher.CFG, 'hide_daemons', True, raising=False)
    rows = [watcher.adapt_remote_row(_row(pid=1, state='daemon', daemon=True), REMOTE, 1000.0),
            watcher.adapt_remote_row(_row(pid=2), REMOTE, 1000.0)]
    assert [s['pid'] for s in watcher.scan_sessions(rows)] == [2]


def test_no_agents_applies_to_remote_rows_without_mutating_the_cache(watcher, monkeypatch):
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    monkeypatch.setattr(watcher.CFG, 'show_agents', False, raising=False)
    cached = watcher.adapt_remote_row(_row(agents=[{'name': 'bob'}]), REMOTE, 1000.0)
    assert watcher.scan_sessions([cached])[0]['agents'] == []
    # La ligne du cache du poller n'a PAS été vidée au passage.
    assert cached['agents'] == [{'pid': None, 'name': 'bob', 'type': None, 'model': None}]


def test_row_keys_do_not_collide_across_machines(watcher):
    """Un pid 1234 local et un pid 1234 distant sont deux process différents."""
    remote_row = watcher.adapt_remote_row(_row(pid=1234), REMOTE, 1000.0)
    local_row = {'pid': 1234}
    assert watcher.session_key(remote_row) != watcher.session_key(local_row)
    assert watcher.session_key(local_row) == '1234'


def test_row_keys_survive_two_labels_that_collide_after_elision(watcher):
    """`build-server-01` et `build-server-02` donnent le MÊME label élidé (12
    caractères) : clé sur le label → DuplicateKey levé par add_row(), donc crash
    sur un tick de rafraîchissement."""
    a, b = watcher.resolve_remotes(
        {}, [('build-server-01', 'https://a/'), ('build-server-02', 'https://b/')], env={})
    assert a['label'] == b['label']            # la collision est bien réelle
    ra = watcher.adapt_remote_row(_row(pid=1), a, 1000.0)
    rb = watcher.adapt_remote_row(_row(pid=1), b, 1000.0)
    assert watcher.session_key(ra) != watcher.session_key(rb)

    # ET le consommateur qui résout une ligne vers l'état de son poller : keyé sur
    # le label, il renvoyait None pour les deux, la ligne perdant en silence son
    # marqueur de péremption, son URL rédigée et son erreur. La clé tenait, le
    # sibling non — et rien ne le testait.
    stat = {a['name']: {'health': 'stale', 'error': 'boom', 'display_url': 'http://a/'},
            b['name']: {'health': 'down', 'error': 'nope', 'display_url': 'http://b/'}}
    assert watcher.remote_rstate(ra, stat, 2.0, 1000.0) is not None
    assert watcher.remote_rstate(rb, stat, 2.0, 1000.0) is not None
    assert watcher.remote_rstate(ra, stat, 2.0, 1000.0) != watcher.remote_rstate(rb, stat, 2.0, 1000.0)


# ── Affichage : préfixe, troncature, péremption ──────────────────────────────

def test_label_prefix_survives_left_truncation(watcher):
    """Le préfixe est réservé HORS du budget : path_display tronque par la GAUCHE,
    donc préfixer après coup effacerait le marqueur."""
    s = watcher.adapt_remote_row(
        _row(display_cwd='/very/long/path/to/some/project'), REMOTE, 1000.0)
    prefix, path = watcher.session_path_cell(s, 20)
    assert prefix == 'lab:'
    assert path.endswith('project')
    assert len(prefix + path) <= 20
    assert '…' in path                     # la troncature a bien eu lieu


def test_local_row_stays_bare(watcher):
    """Local est le cas courant : le baliser taxerait la majorité des lignes."""
    prefix, path = watcher.session_path_cell({'cwd': '/tmp/x', 'display_cwd': '/tmp/x'}, 40)
    assert prefix == ''
    assert path == '/tmp/x'


def test_the_path_budget_never_collapses_to_nothing(watcher):
    s = watcher.adapt_remote_row(_row(display_cwd='/a/b/c/d/e'),
                                 {'name': 'x', 'label': 'a-very-long'}, 1000.0)
    _, path = watcher.session_path_cell(s, 8)
    assert len(path) >= 4


def test_staleness_threshold_is_three_polls(watcher):
    st = {'rows': [], 'received_mono': 1000.0, 'error': None, 'status': None}
    poll_s = 2.0
    assert watcher.remote_health(st, poll_s, 1000.0 + 5.9)[0] == 'ok'
    assert watcher.remote_health(st, poll_s, 1000.0 + 6.0)[0] == 'ok'
    health, age = watcher.remote_health(st, poll_s, 1000.0 + 6.1)
    assert health == 'stale' and age == pytest.approx(6.1)


def test_a_remote_that_never_answered_is_down_not_stale(watcher):
    st = {'rows': [], 'received_mono': None, 'error': 'URLError', 'status': None}
    assert watcher.remote_health(st, 2.0, 1000.0) == ('down', None)


def test_a_remote_whose_first_poll_is_in_flight_reads_starting(watcher):
    """`received_mono is None` sans erreur n'est pas « n'a jamais répondu » :
    c'est « n'a pas encore eu le temps », et l'afficher « injoignable » au
    démarrage est un faux positif systématique."""
    st = {'rows': [], 'received_mono': None, 'error': None, 'status': None}
    assert watcher.remote_health(st, 2.0, 1000.0) == ('starting', None)
    assert watcher.remote_health_text(st, 2.0, 1000.0) == watcher.tr('rm_starting')


def test_auth_failure_is_distinct_from_unreachable(watcher):
    st = {'rows': [], 'received_mono': None, 'error': 'HTTP 401', 'status': 401}
    assert watcher.remote_health(st, 2.0, 1000.0)[0] == 'auth'


def test_a_dead_poll_thread_can_never_read_ok(watcher):
    """Un thread mort après un premier succès lisait « ok » puis « périmé » POUR
    TOUJOURS — un instantané vieux d'un jour indiscernable de deux polls manqués."""
    st = {'rows': [{}], 'received_mono': 1000.0, 'error': None, 'status': None, 'alive': False}
    assert watcher.remote_health(st, 2.0, 1000.5)[0] == 'dead'
    assert watcher.remote_health_text(st, 2.0, 1000.5) == watcher.tr('rm_dead')
    # Contrôle : le même état avec un thread vivant lit bien « ok ».
    assert watcher.remote_health(dict(st, alive=True), 2.0, 1000.5)[0] == 'ok'


def test_status_area_distinguishes_ok_zero_from_down(watcher):
    remote = {'name': 'lab', 'label': 'lab'}
    ok_empty = {'rows': [], 'received_mono': 1000.0, 'status': None}
    never = {'rows': [], 'received_mono': None, 'error': 'timeout', 'status': None}
    assert watcher.remote_status_text(remote, ok_empty, 2.0, 1000.0) == 'lab ok 0'
    assert watcher.remote_status_text(remote, never, 2.0, 1000.0) == 'lab down'


def test_status_area_shows_the_age_when_stale(watcher):
    st = {'rows': [{}], 'received_mono': 1000.0, 'status': None}
    assert watcher.remote_status_text({'name': 'l', 'label': 'lab'}, st, 2.0, 1042.0) \
        == 'lab stale 42s'


def test_status_area_counts_sessions_when_ok(watcher):
    st = {'rows': [{}, {}, {}], 'received_mono': 1000.0, 'status': None}
    assert watcher.remote_status_text({'name': 'l', 'label': 'lab'}, st, 2.0, 1001.0) \
        == 'lab ok 3'


def test_status_area_surfaces_a_truncated_row_flood(watcher):
    st = {'rows': [{}] * 500, 'received_mono': 1000.0, 'status': None, 'total': 612}
    assert watcher.remote_status_text({'name': 'l', 'label': 'lab'}, st, 2.0, 1001.0) \
        == 'lab ok 500/612'


def test_status_bar_lists_every_configured_remote(watcher):
    """Un remote qui n'a JAMAIS répondu n'a aucune ligne à marquer périmée : sans
    cette zone il serait purement invisible."""
    remotes = [{'name': 'lab', 'label': 'lab'}, {'name': 'prod', 'label': 'prod'}]
    stat = {'lab': {'rows': [{}], 'received_mono': 1000.0, 'status': None},
            'prod': {'rows': [], 'received_mono': None, 'error': 'timeout'}}
    bar = watcher.remotes_bar_text(remotes, stat, 2.0, 1000.0)
    assert 'lab ok 1' in bar
    assert 'prod down' in bar


def test_status_bar_tooltip_carries_redacted_url_and_error(watcher):
    remotes = [{'name': 'lab', 'label': 'lab', 'display_url': 'https://remote:***@box/'}]
    stat = {'lab': {'display_url': 'https://remote:***@box/', 'error': 'HTTP 401'}}
    tip = watcher.remotes_bar_tooltip(remotes, stat)
    assert 'https://remote:***@box/' in tip and 'HTTP 401' in tip


def test_status_bar_tooltip_falls_back_to_the_remotes_own_url(watcher):
    """Sans état (aucun poll encore), la TUI affichait une URL VIDE là où GTK
    montre celle du remote."""
    remotes = [{'name': 'lab', 'label': 'lab', 'display_url': 'https://box/'}]
    assert 'https://box/' in watcher.remotes_bar_tooltip(remotes, {})


def test_a_hostile_error_string_cannot_break_the_status_bar_tooltip(watcher):
    """Le texte d'erreur vient du SERVEUR et l'infobulle Textual est un
    Static(markup=True) : un « [/] » y levait MarkupError AU SURVOL, donc un
    remote hostile faisait tomber la TUI. On enveloppe dans Text()."""
    from rich.text import Text
    remotes = [{'name': 'lab', 'label': 'lab'}]
    stat = {'lab': {'display_url': 'https://box/', 'error': '[/] [INST] boom'}}
    raw = watcher.remotes_bar_tooltip(remotes, stat)
    assert '[/]' in raw                      # le markup hostile est bien là…
    assert Text(raw) is not None             # …et Text() est ce qui le neutralise


def test_every_server_controlled_string_reaches_textual_wrapped_in_text(watcher):
    """Les trois sites qui affichent du texte VENU DU SERVEUR doivent envelopper
    dans Text() : Static et l'infobulle sont des `markup=True`, donc un « [/] »
    dans une ligne de statut lève MarkupError — en plein tick pour l'état vide,
    AU SURVOL pour l'infobulle de la barre (un remote hostile faisait tomber la
    TUI). Le site de l'infobulle était le seul resté en `str` brut.

    Contrôle sur la SOURCE : le widget ne peut pas être piloté ici (textual est
    stubé, donc WatcherApp est un MagicMock), et un test qui n'exécute pas la
    ligne ne peut pas la garder.
    """
    import re
    from pathlib import Path
    src = Path(watcher.__file__).read_text()
    body = src.split('def refresh_sessions(self)', 1)[1].split('\n    # ── Version', 1)[0]
    for pattern in (r'empty\.update\(\s*Text\(',
                    r'remotes_bar\.update\(\s*Text\(',
                    r'remotes_bar\.tooltip\s*=\s*Text\('):
        assert re.search(pattern, body), f'texte serveur non enveloppé : {pattern}'


def test_the_tui_clocks_remote_staleness_on_the_monotonic_clock(watcher):
    """Deux horloges, deux métiers : `last_activity` reste MURALE (comparée à
    time.time() au rendu), la péremption d'un remote est MONOTONE.

    Le pendant GTK de ce test existait déjà ; ici le site d'appel n'était gardé
    par rien : remplacer `time.monotonic()` par `time.time()` laissait la suite
    entièrement verte, alors que le mode de panne est sévère — un remote mort
    depuis un jour repasse « ok » après un pas NTP arrière ou une reprise de
    veille (le compteur monotone, lui, ne recule jamais).

    Contrôle sur la SOURCE : textual est stubé, WatcherApp est un MagicMock, on
    ne peut pas piloter le widget ici. Une seule variable alimente les deux
    consommateurs, donc un seul point de mutation à garder.
    """
    import re
    from pathlib import Path
    src = Path(watcher.__file__).read_text()
    body = src.split('def refresh_sessions(self)', 1)[1].split('\n    # ── Version', 1)[0]
    assert re.search(r'^\s*now\s*=\s*time\.time\(\)\s*$', body, re.M), \
        'la durée affichée doit rester en horloge murale'
    assert re.search(r'^\s*now_mono\s*=\s*time\.monotonic\(\)\s*$', body, re.M), \
        'la péremption des remotes doit être mesurée en horloge monotone'
    for pattern in (r'remote_rstate\([^)]*\bnow_mono\b',
                    r'remotes_bar_text\([^)]*\bnow_mono\b'):
        assert re.search(pattern, body), f'horloge murale sur la péremption : {pattern}'
    # Le panneau de configuration n'a pas de now_mono sous la main : il relit
    # l'horloge monotone directement, même contrat.
    assert re.search(r'remotes_panel_rows\([^)]*time\.monotonic\(\)', src)


def test_empty_state_says_which_remotes_failed(watcher):
    """« aucune session active » serait un mensonge quand un remote a échoué."""
    remotes = [{'name': 'lab', 'label': 'lab'}]
    assert watcher.empty_state_text(remotes, {'lab': {'received_mono': 1000.0}}) \
        == watcher.tr('no_session')
    txt = watcher.empty_state_text(remotes, {'lab': {'error': 'HTTP 401'}})
    assert txt != watcher.tr('no_session')
    assert 'lab' in txt and 'HTTP 401' in txt


def test_empty_state_without_any_remote_is_unchanged(watcher):
    assert watcher.empty_state_text([], {}) == watcher.tr('no_session')


def test_stale_marker_carries_the_age_of_the_data(watcher):
    assert watcher.remote_stale_text({'health': 'ok', 'age': 99.0}) is None
    assert watcher.remote_stale_text({'health': 'starting', 'age': None}) is None
    assert watcher.remote_stale_text(None) is None
    assert watcher.remote_stale_text({'health': 'stale', 'age': 42.0}).endswith('42s')
    assert watcher.remote_stale_text({'health': 'down', 'age': None})


def test_row_tooltip_shows_the_redacted_url_and_the_error(watcher):
    s = watcher.adapt_remote_row(_row(), REMOTE, 1000.0)
    rstate = {'health': 'stale', 'age': 42.0, 'error': 'HTTP 401',
              'display_url': 'https://remote:***@box:8000/'}
    tip = watcher.session_tooltip(s, rstate)
    assert 'https://remote:***@box:8000/' in tip
    assert 'HTTP 401' in tip
    assert '42s' in tip
    assert 'lab' in tip                       # le label de la machine
    # Une ligne locale ne gagne aucun de ces blocs.
    assert 'HTTP 401' not in watcher.session_tooltip(dict(s, remote=None), None)


def test_row_tooltip_lists_the_subagents(watcher):
    s = watcher.adapt_remote_row(_row(agents=[{'name': 'bob', 'type': 't', 'model': 'm'}]),
                                 REMOTE, 1000.0)
    assert 'bob (t, m)' in watcher.session_tooltip(s, None)


def test_rstate_is_none_for_a_local_row(watcher):
    assert watcher.remote_rstate({'pid': 1}, {}, 2.0, 1000.0) is None


def test_rstate_carries_health_age_and_redacted_url(watcher):
    s = watcher.adapt_remote_row(_row(), REMOTE, 1000.0)
    stat = {'lab': {'rows': [], 'received_mono': 1000.0, 'error': None, 'status': None,
                    'display_url': 'https://remote:***@box/'}}
    rstate = watcher.remote_rstate(s, stat, 2.0, 1042.0)
    assert rstate['health'] == 'stale' and rstate['age'] == pytest.approx(42.0)
    assert rstate['display_url'] == 'https://remote:***@box/'


def test_settings_panel_lists_disabled_remotes_too(watcher):
    """Un remote éteint doit rester VISIBLE dans les paramètres : sinon rien ne
    dit qu'il a bien été analysé."""
    remotes = watcher.resolve_remotes(
        {'lab': {'url': 'https://remote:s3cr3t@box/'},
         'old': {'url': 'https://old/', 'enabled': 'no'}}, [], env={})
    rows = watcher.remotes_panel_rows(
        remotes, {'lab': {'rows': [], 'received_mono': 1000.0}}, 2.0, 1000.0)
    assert [r[0] for r in rows] == ['lab', 'old']
    assert all('s3cr3t' not in r[1] for r in rows)
    assert rows[0][2] == 'ok 0'
    assert rows[1][2] == watcher.tr('off')


# ── fetch_remote : transport, plafond, erreurs ───────────────────────────────

class _Resp:
    """Réponse factice qui se CONSOMME (comme une vraie socket) et enregistre
    chaque taille demandée — c'est ce qui rend le plafond de lecture testable :
    un stub dont read(n) renvoie toujours tout le corps laisse passer un read()
    non borné."""

    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0
        self.reads: list[int] = []

    def read(self, n=-1):
        self.reads.append(n)
        if n is None or n < 0:
            chunk, self._pos = self._body[self._pos:], len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_sends_the_token_as_a_header_not_in_the_url(watcher):
    """Le token ne part JAMAIS dans une URL que le client CONSTRUIT.

    Le webui ne lit plus que les en-têtes, et son middleware d'accès journalise
    `query_params` à chaque requête : un token glissé dans l'URL serait à la fois
    refusé (401) et écrit en clair dans le log du serveur, à chaque poll. Donc,
    d'un `https://remote:<token>@hote/` : l'URL résolue puis celle construite par
    remote_endpoint() sont PROPRES — ni userinfo, ni query — et le secret ne
    voyage que dans `X-API-Key`.
    """
    seen = {}

    def opener(req, timeout=None):
        seen['url'] = req.full_url
        seen['host'] = req.host
        seen['key'] = req.get_header('X-api-key')
        seen['timeout'] = timeout
        return _Resp(json.dumps({'sessions': [], 'age_seconds': 0.0}).encode())

    remote = watcher.resolve_remotes({}, [('lab', 'https://remote:s3cr3t@box:8000/')], env={})[0]
    assert 's3cr3t' not in remote['url'] and '@' not in remote['url']
    assert watcher.remote_endpoint(remote['url']) == 'https://box:8000/api/sessions'
    rows, error, status, total = watcher.fetch_remote(remote, opener=opener)
    assert (rows, error, status, total) == ([], None, None, 0)
    assert seen['url'] == 'https://box:8000/api/sessions'
    assert seen['host'] == 'box:8000'          # pas de userinfo : urllib ne sait pas le traiter
    # Rien du secret nulle part dans l'URL émise : ni en query, ni en userinfo,
    # ni dans le chemin. Le `?` interdit couvre aussi une query qu'on ajouterait.
    assert 's3cr3t' not in seen['url'] and '?' not in seen['url'] and '@' not in seen['url']
    assert seen['key'] == 's3cr3t'
    assert seen['timeout'] == watcher.REMOTE_TIMEOUT_S


def test_fetch_without_token_sends_no_header(watcher):
    seen = {}

    def opener(req, timeout=None):
        seen['key'] = req.get_header('X-api-key')
        return _Resp(b'{"sessions": []}')

    remote = watcher.resolve_remotes({}, [('lab', 'https://box/')], env={})[0]
    watcher.fetch_remote(remote, opener=opener)
    assert seen['key'] is None


@pytest.mark.parametrize('url, expected', [
    ('https://box:8000',       'https://box:8000/api/sessions'),
    ('https://box:8000/',      'https://box:8000/api/sessions'),
    ('https://box/sub/',       'https://box/sub/api/sessions'),
    # La query reçue est reportée telle quelle : le client ne réécrit pas l'URL
    # qu'on lui a donnée. Ce n'est PAS de l'auth — le webui ne lit plus la query
    # — mais un reverse proxy devant lui peut exiger ses propres paramètres.
    ('https://box/?key=tok',   'https://box/api/sessions?key=tok'),
])
def test_the_endpoint_is_joined_on_the_path_not_the_whole_url(watcher, url, expected):
    """`'https://box/?x=1'.rstrip('/') + '/api/sessions'` mettait le endpoint
    DANS la query : /api/sessions n'était jamais demandé."""
    assert watcher.remote_endpoint(url) == expected


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://box/', 'myhost:8000',
                                 'myhost'])
def test_only_http_and_https_are_polled(watcher, url):
    """`file://` est lu par l'ouvreur par défaut d'urllib : une faute de frappe
    deviendrait une lecture de fichier local rendue comme des sessions vivantes."""
    with pytest.raises(ValueError):
        watcher.remote_endpoint(url)


def test_a_scheme_less_url_records_an_error_and_does_not_raise(watcher):
    """`--remote lab=myhost` (schéma oublié) levait ValueError HORS du try : le
    thread mourait au premier tour SANS rien enregistrer, donc infobulle vide et
    « aucune session active » pour un remote mal configuré."""
    remote = {'name': 'lab', 'label': 'lab', 'url': 'myhost', 'enabled': True}
    rows, error, status, total = watcher.fetch_remote(
        remote, opener=lambda r, timeout=None: pytest.fail('aucune requête ne doit partir'))
    assert rows == [] and status is None and total == 0
    assert error is not None and 'ValueError' in error   # l'erreur EST enregistrée


def test_print_once_survives_a_scheme_less_remote(watcher, monkeypatch, capsys):
    """La même levée faisait planter --once tout court, emportant les sessions
    LOCALES avec elle."""
    monkeypatch.setattr(watcher.CFG, 'no_local', False, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    monkeypatch.setattr(watcher.CFG, 'hide_daemons', False, raising=False)
    monkeypatch.setattr(watcher.CFG, 'show_agents', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'idle_format', 'none', raising=False)
    monkeypatch.setattr(watcher, 'scan_local_sessions',
                        lambda: [dict(_row(pid=5), waiting=False, working=False,
                                      background=False, project='local', worktree=None,
                                      topic=None, agents=[], config_dir=None,
                                      last_activity=None, context_pct=None, tool=None,
                                      daemon=False, display_cwd='/home/u/local')])
    watcher.print_once([{'name': 'lab', 'label': 'lab', 'url': 'myhost', 'enabled': True}])
    out = capsys.readouterr().out
    assert 'lab:' in out                       # le remote en échec est signalé…
    assert '/home/u/local' in out              # …et la session locale est bien listée


def test_print_once_never_claims_no_session_after_listing_failed_remotes(
        watcher, monkeypatch, capsys):
    """« aucune session active » juste après avoir listé un remote en échec est
    exactement le mensonge que la spec interdit."""
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], 'HTTP 401', 401, 0))
    watcher.print_once([{'name': 'lab', 'label': 'lab', 'url': 'https://box/',
                         'enabled': True, 'display_url': 'https://box/'}])
    out = capsys.readouterr().out
    assert watcher.tr('no_session') not in out
    assert 'lab' in out and 'HTTP 401' in out


def test_print_once_says_no_session_when_nothing_failed(watcher, monkeypatch, capsys):
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    watcher.print_once([])
    assert watcher.tr('no_session') in capsys.readouterr().out


def test_print_once_marks_the_path_like_the_live_view(watcher, monkeypatch, capsys):
    """Le label balisait le PROJET ici et le CHEMIN dans la vue live : les deux
    vues se contredisaient sur la convention scp."""
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    monkeypatch.setattr(watcher.CFG, 'idle_format', 'none', raising=False)
    row = watcher.adapt_remote_row(_row(display_cwd='/home/u/proj'), REMOTE, time.time())
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([row], None, None, 1))
    watcher.print_once([{'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}])
    assert 'lab:/home/u/proj' in capsys.readouterr().out


def test_oversized_response_is_rejected(watcher):
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    big = b'x' * (watcher.REMOTE_MAX_BYTES + 1)
    rows, error, status, _ = watcher.fetch_remote(
        remote, opener=lambda r, timeout=None: _Resp(big))
    assert rows == [] and status is None and 'MiB' in error


def test_the_read_is_bounded_at_every_call_not_just_in_total(watcher):
    """Un read() non borné passait les deux suites : le stub renvoyait tout le
    corps quel que soit `n`. On enregistre les tailles demandées et on les
    vérifie."""
    resp = _Resp(b'x' * (watcher.REMOTE_MAX_BYTES + 1))
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    watcher.fetch_remote(remote, opener=lambda r, timeout=None: resp)
    assert resp.reads, 'aucune lecture effectuée'
    assert all(0 < n <= watcher.REMOTE_READ_CHUNK for n in resp.reads)
    assert sum(resp.reads) <= watcher.REMOTE_MAX_BYTES + 1


def test_a_dribbling_peer_cannot_park_the_thread_forever(watcher, monkeypatch):
    """`timeout=5` est PAR OPÉRATION socket : un pair qui livre un octet toutes
    les 4 s ne le déclenche jamais et défait stop(). Budget total en monotone."""
    monkeypatch.setattr(watcher, 'REMOTE_READ_BUDGET_S', 0.05)

    class Dribble:
        def read(self, n=-1):
            time.sleep(0.02)
            return b'x'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    started = time.monotonic()
    rows, error, _, _ = watcher.fetch_remote(remote, opener=lambda r, timeout=None: Dribble())
    assert rows == [] and error
    assert time.monotonic() - started < 2.0


def test_http_401_is_reported_with_its_status(watcher):
    import email.message
    import urllib.error

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized',
                                     email.message.Message(), None)

    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    rows, error, status, _ = watcher.fetch_remote(remote, opener=opener)
    assert (rows, status) == ([], 401) and error == 'HTTP 401'


@pytest.mark.parametrize('boom', [
    OSError('unreachable'),
    ValueError('nope'),
    TimeoutError('timed out'),
])
def test_no_exception_escapes_the_fetch(watcher, boom):
    def opener(req, timeout=None):
        raise boom

    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    rows, error, status, _ = watcher.fetch_remote(remote, opener=opener)
    assert rows == [] and status is None and error


def test_invalid_json_is_an_error_not_a_crash(watcher):
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/'}
    rows, error, _, _ = watcher.fetch_remote(
        remote, opener=lambda r, timeout=None: _Resp(b'<html>not json</html>'))
    assert rows == [] and error


def test_redirects_are_not_followed(watcher):
    """L'ouvreur par défaut d'urllib REJOUE les en-têtes (donc X-API-Key) vers la
    cible d'une 302, autre hôte et https→http compris : un remote compromis
    exfiltrerait le token. On refuse de suivre."""
    import http.server

    stolen = {}

    class Sink(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            stolen['key'] = self.headers.get('X-API-Key')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"sessions": []}')

        def log_message(self, *a):
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{sink.server_address[1]}/steal')
            self.end_headers()

        def log_message(self, *a):
            pass

    sink = http.server.HTTPServer(('127.0.0.1', 0), Sink)
    red = http.server.HTTPServer(('127.0.0.1', 0), Redirector)
    for srv in (sink, red):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        remote = watcher.resolve_remotes(
            {}, [('lab', f'http://remote:s3cr3t@127.0.0.1:{red.server_address[1]}/')], env={})[0]
        rows, error, status, _ = watcher.fetch_remote(remote)
        assert stolen == {}, 'le token a été rejoué vers l’hôte de la redirection'
        assert rows == [] and status == 302 and error == 'HTTP 302'
    finally:
        sink.shutdown()
        red.shutdown()


def test_a_hostile_status_line_cannot_inject_into_the_terminal(watcher):
    """Le texte d'erreur vient du SERVEUR : http.client met la ligne de statut
    brute dans BadStatusLine, échappements ANSI compris, et elle finit à l'écran."""
    import socketserver

    class Evil(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(b'\x1b[2J\x1b[31mPWNED status line\r\n\r\n')

    srv = socketserver.TCPServer(('127.0.0.1', 0), Evil)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        remote = {'name': 'lab', 'label': 'lab',
                  'url': f'http://127.0.0.1:{srv.server_address[1]}/'}
        _, error, _, _ = watcher.fetch_remote(remote)
        assert error and '\x1b' not in error and '\r' not in error and '\n' not in error
    finally:
        srv.shutdown()


@pytest.mark.parametrize('url', [
    'https://remote:s3cr3t@box/',   # userinfo : split_remote_url l'a déjà retiré de l'URL
    'https://box/?key=s3cr3t',      # query : reste sur le fil, le client ne réécrit pas l'URL
])
def test_fetch_never_leaks_the_token_in_its_error(watcher, url):
    """Les DEUX formes, pas seulement celle qui n'était plus à risque.

    Testé uniquement sur le userinfo, ce garde passait sur la seule forme que
    `split_remote_url` avait déjà neutralisée. Une valeur en query, elle, reste
    dans l'URL interrogée — on ne réécrit pas l'URL qu'on nous a donnée, même si
    le webui n'y lit plus rien — donc elle atteint bien le message d'erreur.
    """
    def opener(req, timeout=None):
        raise OSError(f'connection to {req.full_url} failed')

    remote = watcher.resolve_remotes({}, [('lab', url)], env={})[0]
    _, error, _, _ = watcher.fetch_remote(remote, opener=opener)
    assert 's3cr3t' not in error


def test_fetch_error_from_a_malformed_url_is_redacted(watcher):
    # Aucun serveur hostile requis : un espace collé dans le token suffit, urllib
    # cite alors l'URL entière dans InvalidURL.
    remote = watcher.resolve_remotes({}, [('lab', 'http://box:8000/?key=s3 cr3t')], env={})[0]
    _, error, _, _ = watcher.fetch_remote(remote)
    assert 's3 cr3t' not in error and '***' in error


# ── Poller ───────────────────────────────────────────────────────────────────

def test_no_remote_means_no_thread(watcher):
    poller = watcher.RemotePoller([], poll_ms=2000)
    assert poller.remotes == [] and poller.sessions() == [] and poller.snapshot() == {}


def test_start_spawns_one_daemon_thread_per_remote_and_stop_ends_them(watcher, monkeypatch):
    """Un corps de start() vide passait le test précédent."""
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], None, None, 0))
    remotes = [{'name': 'a', 'label': 'a', 'url': 'https://a/', 'enabled': True},
               {'name': 'b', 'label': 'b', 'url': 'https://b/', 'enabled': True}]
    poller = watcher.RemotePoller(remotes, poll_ms=250)
    before = {t.name for t in threading.enumerate()}
    poller.start()
    try:
        mine = [t for t in threading.enumerate()
                if t.name in ('remote-a', 'remote-b') and t.name not in before]
        assert len(mine) == 2
        assert all(t.daemon for t in mine)
    finally:
        poller.stop()
    for t in mine:
        t.join(timeout=5)
        assert not t.is_alive(), f'{t.name} a survécu à stop()'


def test_poller_keeps_the_last_good_rows_on_failure(watcher, monkeypatch):
    """Jeter les lignes ferait clignoter la liste au moindre poll manqué."""
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=1000)
    row = watcher.adapt_remote_row(_row(), remote, 1000.0)
    calls = [([row], None, None, 1), ([], 'URLError', None, 0)]
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: calls.pop(0))
    # Deux tours (un bon, un raté) puis arrêt : _loop sort dès que _stop est armé.
    monkeypatch.setattr(poller._stop, 'wait', lambda d: calls or poller._stop.set())
    poller._loop(remote)
    st = poller.snapshot()['lab']
    assert st['rows'] == [row]                 # conservées
    assert st['error'] == 'URLError'
    assert poller.sessions() == [row]


def test_staleness_is_stamped_on_the_monotonic_clock(watcher, monkeypatch):
    """La péremption est TOUT le contrat : en horloge murale, un pas NTP arrière
    ou un portable qui sort de veille rendait un remote mort « frais »."""
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=1000)
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], None, None, 0))
    monkeypatch.setattr(poller._stop, 'wait', lambda d: poller._stop.set())
    # Horloge murale figée dans le futur : si la péremption la lisait, l'écart
    # avec time.monotonic() serait énorme.
    monkeypatch.setattr(watcher.time, 'time', lambda: 4_000_000_000.0)
    poller._loop(remote)
    st = poller.snapshot()['lab']
    assert st['received_mono'] == pytest.approx(time.monotonic(), abs=5)
    # alive=True : _loop vient de rendre la main, donc le thread est marqué mort
    # — on interroge la santé telle qu'elle se lit pendant que la boucle tourne.
    assert watcher.remote_health(dict(st, alive=True), 1.0, time.monotonic())[0] == 'ok'


def test_a_raising_callback_never_kills_the_poll_thread(watcher, monkeypatch):
    """La docstring de _loop promet « ne lève JAMAIS » et rien ne le garantissait :
    une levée terminait le thread, et comme received_mono était déjà posé le
    remote lisait « ok » puis « périmé » pour toujours."""
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    ticks = []

    def boom():
        raise RuntimeError('[/] notify exploded')

    poller = watcher.RemotePoller([remote], poll_ms=1000, notify=boom)
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], None, None, 0))
    monkeypatch.setattr(poller._stop, 'wait',
                        lambda d: ticks.append(d) or (len(ticks) >= 3 and poller._stop.set()))
    poller._loop(remote)                        # ne doit PAS lever
    assert len(ticks) == 3                      # la boucle a survécu à trois levées
    st = poller.snapshot()['lab']
    assert st['error'] and 'RuntimeError' in st['error']
    assert '\x1b' not in st['error']            # nettoyée comme tout texte affiché


def test_a_thread_that_exits_marks_itself_not_alive(watcher, monkeypatch):
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=1000)
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], None, None, 0))
    monkeypatch.setattr(poller._stop, 'wait', lambda d: poller._stop.set())
    poller._loop(remote)
    st = poller.snapshot()['lab']
    assert st['alive'] is False
    assert watcher.remote_health(st, 1.0, time.monotonic())[0] == 'dead'


def test_disabled_remotes_are_filtered_before_the_poller(watcher):
    remotes = [{'name': 'a', 'label': 'a', 'url': 'u', 'enabled': False},
               {'name': 'b', 'label': 'b', 'url': 'u', 'enabled': True}]
    poller = watcher.RemotePoller(watcher.enabled_remotes(remotes))
    assert [r['name'] for r in poller.remotes] == ['b']


def test_every_poller_construction_site_filters_on_enabled(watcher):
    """Le poller ne refiltre plus (le filtre vit à UN seul endroit) : tout site
    qui le construit doit donc filtrer lui-même, sinon un remote explicitement
    désactivé serait interrogé quand même."""
    import re
    from pathlib import Path
    src = Path(watcher.__file__).read_text()
    for call in re.findall(r'RemotePoller\((.*?)[,)]', src):
        if 'remotes: list' in call:          # la signature elle-même
            continue
        assert call.strip() in ('remotes',), f'site non filtré : RemotePoller({call})'
    # …et les deux appelants passent bien une liste filtrée.
    assert 'enabled = enabled_remotes(CFG.remotes)' in src
    assert 'remotes=enabled_remotes(' in src


def test_auth_failure_is_not_retried_tightly(watcher, monkeypatch):
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=500)
    delays = []
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], 'HTTP 401', 401, 0))
    monkeypatch.setattr(poller._stop, 'wait',
                        lambda d: delays.append(d) or poller._stop.set())
    poller._loop(remote)
    assert delays == [watcher.REMOTE_AUTH_RETRY_S]


def test_the_auth_floor_is_above_the_backoff_cap(watcher):
    """Égales, la constante ne pouvait JAMAIS changer le comportement."""
    assert watcher.REMOTE_AUTH_RETRY_S > watcher.REMOTE_BACKOFF_MAX_S


def test_repeated_failures_back_off_and_the_cap_holds(watcher, monkeypatch):
    """Quatre itérations plafonnaient à 16 s, sous la limite : le plafond
    lui-même n'était pas testé."""
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=2000)
    delays = []
    monkeypatch.setattr(watcher, 'fetch_remote', lambda r, **k: ([], 'URLError', None, 0))
    monkeypatch.setattr(poller._stop, 'wait',
                        lambda d: delays.append(d) or (len(delays) >= 10 and poller._stop.set()))
    poller._loop(remote)
    assert delays == sorted(delays) and delays[0] < delays[-1]
    assert max(delays) == watcher.REMOTE_BACKOFF_MAX_S     # le plafond est ATTEINT
    assert len(delays) == 10                               # et il tient au-delà de 6 tours


def test_snapshot_is_a_copy(watcher):
    poller = watcher.RemotePoller([{'name': 'a', 'label': 'a', 'url': 'u'}])
    snap = poller.snapshot()
    snap['a']['rows'] = ['tampered']
    assert poller.snapshot()['a']['rows'] == []


def test_the_poll_interval_has_a_floor(watcher):
    assert watcher.RemotePoller([], poll_ms=1).poll_s == watcher.REMOTE_POLL_MIN_MS / 1000


# ── Aucun HTTP dans le chemin de rafraîchissement ────────────────────────────

def test_the_merge_path_never_performs_http(watcher, monkeypatch):
    """`refresh_sessions()` tourne dans la boucle UI : un seul appel réseau y
    figerait l'écran. On casse fetch_remote et on déroule tout le chemin de
    fusion + d'affichage."""
    monkeypatch.setattr(watcher, 'fetch_remote',
                        lambda *a, **k: pytest.fail('HTTP depuis le chemin de rafraîchissement'))
    monkeypatch.setattr(watcher.CFG, 'no_local', True, raising=False)
    monkeypatch.setattr(watcher.CFG, 'sort_mode', 'default', raising=False)
    remote = {'name': 'lab', 'label': 'lab', 'url': 'https://box/', 'enabled': True}
    poller = watcher.RemotePoller([remote], poll_ms=1000)
    with poller._lock:                        # simule un poll déjà revenu
        poller._state['lab'].update(rows=[watcher.adapt_remote_row(_row(), remote, 1000.0)],
                                    received_mono=time.monotonic(), total=1, alive=True)
    rows = poller.sessions()
    stat = poller.snapshot()
    sessions = watcher.scan_sessions(rows)
    assert len(sessions) == 1
    now_mono = time.monotonic()
    watcher.session_path_cell(sessions[0], 40)
    watcher.local_config_dirs(sessions)
    watcher.session_tooltip(sessions[0], watcher.remote_rstate(sessions[0], stat, 1.0, now_mono))
    assert 'lab ok 1' in watcher.remotes_bar_text(poller.remotes, stat, 1.0, now_mono)
    watcher.remotes_bar_tooltip(poller.remotes, stat)
    watcher.empty_state_text(poller.remotes, stat)


# ── Config : chargement, écriture, permissions ───────────────────────────────

@pytest.fixture
def cfg_file(watcher, monkeypatch, tmp_path):
    path = tmp_path / 'config.ini'
    monkeypatch.setattr(watcher, 'CONFIG_PATH', path)
    monkeypatch.setattr(watcher, 'CONFIG_DIR', tmp_path)
    return path


def test_remote_sections_are_loaded(watcher, cfg_file):
    cfg_file.write_text(
        '[remotes]\npoll_ms = 5000\n\n'
        '[remote:lab]\nurl = https://box:8000/\ntoken = s3cr3t\nlabel = Labo\n')
    conf = watcher.load_config()
    assert conf['remote_poll_ms'] == 5000
    assert conf['remote_sections'] == {
        'lab': {'url': 'https://box:8000/', 'token': 's3cr3t', 'label': 'Labo'}}


def test_no_remote_section_means_no_remote(watcher, cfg_file):
    cfg_file.write_text('[display]\nrefresh_ms = 1000\n')
    conf = watcher.load_config()
    assert conf['remote_sections'] == {}
    assert conf['remote_poll_ms'] == watcher.REMOTE_POLL_MS


def test_poll_ms_has_a_floor(watcher, cfg_file):
    cfg_file.write_text('[remotes]\npoll_ms = 5\n')
    assert watcher.load_config()['remote_poll_ms'] == watcher.REMOTE_POLL_MIN_MS


def test_a_garbage_poll_ms_falls_back_to_the_default(watcher, cfg_file):
    cfg_file.write_text('[remotes]\npoll_ms = souvent\n')
    assert watcher.load_config()['remote_poll_ms'] == watcher.REMOTE_POLL_MS


def test_an_empty_remote_section_name_is_ignored(watcher, cfg_file):
    cfg_file.write_text('[remote:]\nurl = https://box/\n')
    assert watcher.load_config()['remote_sections'] == {}


def test_save_config_forces_0600_on_an_existing_world_readable_file(watcher, cfg_file):
    """`touch(mode=0600, exist_ok=True)` NE re-chmode PAS un fichier existant :
    sur le chemin de mise à niveau (config.ini 0644 d'avant les remotes, le cas
    courant), le token était écrit lisible par tous."""
    cfg_file.write_text('[display]\ncards = false\n')
    cfg_file.chmod(0o644)
    watcher.save_config({'remote:lab': {'token': 's3cr3t'}})
    assert stat_mod.S_IMODE(cfg_file.stat().st_mode) == 0o600


def test_save_config_creates_the_file_0600(watcher, cfg_file):
    watcher.save_config({'remote:lab': {'token': 's3cr3t'}})
    assert stat_mod.S_IMODE(cfg_file.stat().st_mode) == 0o600


def test_save_config_preserves_the_other_sections(watcher, cfg_file):
    cfg_file.write_text('[display]\ncards = true\n\n[remote:lab]\nurl = https://box/\n')
    watcher.save_config({'display': {'sort_mode': 'idle'}})
    text = cfg_file.read_text()
    assert 'cards = true' in text
    assert '[remote:lab]' in text and 'url = https://box/' in text
    assert 'sort_mode = idle' in text


# ── Câblage de main() ────────────────────────────────────────────────────────

def test_main_wires_the_sections_and_the_flags_together(watcher, monkeypatch, cfg_file):
    seen = {}
    monkeypatch.setattr(watcher, 'CFG', watcher.CFG)     # restauré après le test
    cfg_file.write_text('[remote:lab]\nurl = https://box/\ntoken = s3cr3t\n')

    def spy(sections, flags, env=None):
        seen['sections'], seen['flags'] = sections, flags
        return []

    monkeypatch.setattr(watcher, 'resolve_remotes', spy)
    monkeypatch.setattr(watcher, 'print_once', lambda remotes=None: None)
    monkeypatch.setattr(sys, 'argv',
                        ['claude-watcher-tui', '--once', '--no-local',
                         '--remote', 'box=https://other/'])
    watcher.main()
    assert seen['sections'] == {'lab': {'url': 'https://box/', 'token': 's3cr3t'}}
    assert seen['flags'] == [('box', 'https://other/')]


def test_a_flag_declared_remote_is_never_persisted(watcher, monkeypatch, cfg_file):
    """`--remote` est pour « montre-moi cette machine une fois » : le config.ini
    ne doit pas bouger d'un octet."""
    monkeypatch.setattr(watcher, 'CFG', watcher.CFG)
    cfg_file.write_text('[display]\ncards = false\n')
    before = cfg_file.read_text()
    monkeypatch.setattr(watcher, 'print_once', lambda remotes=None: None)
    monkeypatch.setattr(sys, 'argv',
                        ['claude-watcher-tui', '--once', '--no-local',
                         '--remote', 'lab=https://remote:s3cr3t@box/'])
    watcher.main()
    assert cfg_file.read_text() == before
    assert 's3cr3t' not in cfg_file.read_text()


def test_main_refuses_to_start_on_a_broken_enabled_value(watcher, monkeypatch, cfg_file):
    """Refuser bruyamment plutôt que retomber sur « activé » : le mode de panne
    d'une faute de frappe ne doit pas être « ton token part quand même »."""
    monkeypatch.setattr(watcher, 'CFG', watcher.CFG)
    cfg_file.write_text('[remote:lab]\nurl = https://box/\nenabled = maybe\n')
    monkeypatch.setattr(sys, 'argv', ['claude-watcher-tui', '--once'])
    with pytest.raises(SystemExit) as e:
        watcher.main()
    assert 'enabled' in str(e.value)
