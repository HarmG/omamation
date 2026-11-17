from omamation import cli


def test_skill_install_links_to_the_package(tmp_path):
    assert cli.main(["skill", "install", "--dir", str(tmp_path)]) == 0
    target = tmp_path / "omamation"
    assert target.is_symlink() and (target / "SKILL.md").exists()
    # Installing again replaces the link instead of failing.
    assert cli.main(["skill", "install", "--dir", str(tmp_path)]) == 0


def test_skill_install_copy(tmp_path):
    assert cli.main(["skill", "install", "--dir", str(tmp_path), "--copy"]) == 0
    target = tmp_path / "omamation"
    assert not target.is_symlink() and (target / "SKILL.md").read_text().startswith("---")
