"""Server-side nav rendering on the Pi (defect B): admin links must be OMITTED for
view-only viewers, mirroring the edge's contract_gen::render_nav. render_header() is the
Pi seam; the cross-language render itself is pinned in tests/test_contract.py."""
from hardware import portal as pt

PARTIAL = '<nav id="portal-nav" class="portal"><!--NAV_LINKS--></nav>'
ADMIN = ("nav-devices", "nav-settings", "nav-costs", "nav-logs", "nav-storage")
VIEWER = ("nav-dashboard", "nav-history", "nav-timelapse", "nav-alarms", "nav-help")


def test_render_header_admin_shows_all_links():
    out = pt.render_header(PARTIAL, "nav-costs", "G", view_only=False)
    for nid in ADMIN + VIEWER:
        assert f'id="{nid}"' in out, nid
    assert 'id="nav-costs" class="active"' in out


def test_render_header_view_only_omits_admin_links():
    # The security boundary: viewers get the admin links removed from the HTML, not hidden.
    out = pt.render_header(PARTIAL, "nav-dashboard", "G", view_only=True)
    for nid in ADMIN:
        assert f'id="{nid}"' not in out, f"view-only leaked admin link {nid}"
    for nid in VIEWER:
        assert f'id="{nid}"' in out, nid


def test_render_header_defaults_to_admin():
    # Default (Pi portal) keeps every link — admins on the LAN navigate the full app.
    out = pt.render_header(PARTIAL, "nav-dashboard", "G")
    assert all(f'id="{nid}"' in out for nid in ADMIN + VIEWER)
