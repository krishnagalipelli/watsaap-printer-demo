"""Setup's printer step says what actually went wrong.

At a counter that already had 2.0.7, installing 2.0.8 stopped on "Runtime
error (at 18:851): The printer could not be created. Check that Microsoft
Print to PDF is enabled" -- one fixed sentence for every failure, with the
PowerShell output hidden and not logged. These hold the two scripts to the
contract that replaced it: the script logs, writes its reason where setup
looks, and setup shows that reason.
"""

from __future__ import annotations

from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "installer"
SETUP = (INSTALLER / "setup.iss").read_text(encoding="utf-8")
PROVISION = (INSTALLER / "provision.ps1").read_text(encoding="utf-8")


def test_setup_reads_the_reason_from_where_the_script_writes_it():
    assert "'provision-error.txt'" in PROVISION
    assert r"{#DataDir}\logs\provision-error.txt" in SETUP
    assert "'C:\\ProgramData\\WAPrinter\\logs'" in PROVISION
    assert '#define DataDir        "C:\\ProgramData\\WAPrinter"' in SETUP


def test_the_fixed_misleading_sentence_is_gone():
    assert "Check that Microsoft Print to PDF is enabled, then run setup again" not in SETUP


def test_a_silent_update_is_not_held_by_a_message_box():
    step = SETUP[SETUP.index("procedure CurStepChanged") :]
    step = step[: step.index("\nend;") ]
    assert "RaiseException(" not in step
    assert "MsgBox(" in step
    assert all(
        line.strip().startswith("SuppressibleMsgBox(")
        for line in step.splitlines()
        if "MsgBox(" in line
    )


def test_the_script_reports_failure_by_exit_code_and_keeps_a_log():
    assert "Start-Transcript" in PROVISION
    assert PROVISION.rstrip().endswith("exit $exitCode")


def test_an_upgrade_leaves_a_queue_that_is_already_right_alone():
    modern = PROVISION[PROVISION.index("function Install-ModernQueue") :]
    assert "is already set up on" in modern
    assert "Reset-ModernPrinter" in modern


def test_the_spooler_is_started_before_anything_asks_it_for_a_driver():
    install = PROVISION[PROVISION.index("function Install-WhatsAppPrinter") :]
    assert install.index("Start-Spooler") < install.index("Install-ModernQueue")


def test_a_broken_printmanagement_provider_is_repaired_or_routed_around():
    """DESKTOP-TO86GMS, 2.0.8: Get-PrinterPort said "Invalid namespace", the
    hidden Get-PrinterDriver returned nothing, and setup blamed Print to PDF,
    which was enabled."""
    install = PROVISION[PROVISION.index("function Install-WhatsAppPrinter") :]
    install = install[: install.index("\n}\n")]
    assert "(Test-PrintManagement) -or (Repair-PrintManagement)" in install
    assert "Install-ClassicPdfQueue" in install
    assert "PrintManagementProvider.mof" in PROVISION
    assert "Get-Printer -ErrorAction Stop" in PROVISION


def test_the_fallback_finds_the_driver_without_the_printmanagement_cmdlets():
    classic = PROVISION[PROVISION.index("function Install-ClassicPdfQueue") :]
    classic = classic[: classic.index("\n}\n")]
    assert "Get-WmiDriverName" in classic
    assert "Get-PrinterDriver" not in classic


def test_uninstall_also_works_without_the_printmanagement_cmdlets():
    uninstall = PROVISION[PROVISION.index("function Uninstall-WhatsAppPrinter") :]
    assert "-not (Test-PrintManagement)" in uninstall
