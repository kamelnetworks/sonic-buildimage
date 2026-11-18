HPE R1N26A front-panel LED mapping

custom_led_hpe_r1n26a.bin is selected only for EEPROM Label R1N26A with
Questone_2 as the selected SKU. The generic Questone_2 and Questone_2A LED
programs remain unchanged. R1N25A is still recognized as Questone_2, but is
not assigned this program without hardware validation of its LED chain.

The HPE unit has the Questone_2 physical lane map and the Questone_2A serial
LED output layout. The 308-byte program preserves the first 228 bytes of
the existing Questone_2A image and translates the final 80-byte table by
matching Ethernet names and lane positions between the two port_config.ini
files. The original table order is SFP1..48, QSFP5..8, then QSFP1..4. This
retains one table entry for each QSFP lane, including in 4x25G layouts.

Reproduce from the repository root:
  python3 device/celestica/x86_64-cel_questone_2-r0/scripts/generate_hpe_r1n26a_led.py
Verify without writing:
  python3 device/celestica/x86_64-cel_questone_2-r0/scripts/generate_hpe_r1n26a_led.py --check

SHA256:
  Source Questone_2A/custom_led.bin:
  0ac3231e3cb2f05cb78e7715b6a940193eaced4dcece3a2e3d74984578ba46c9
  Generated custom_led_hpe_r1n26a.bin:
  6af4180b7f4a8bda9833a0536b1dd6a00bfac7cd055e225fbdadeb28398ea73b
The generator rejects changed source instructions or unexpected port maps.

The init selector runs after normal SKU selection, including on an upgrade
that retains default_sku. It only changes an absent LED link or a known
stock Questone_2/Questone_2A LED symlink. It preserves existing SKU/JSON
choices, regular LED files and links to other custom programs. An existing
incorrect default_sku=Questone_2A is not silently rewritten by this LED-only
change; it needs separate platform-selection diagnosis.
Modifying a stock target file while retaining its stock symlink does not
mark it as a custom selection; that link is still migrated to the HPE file.

Hardware evidence (2026-11-18): one HPE FM3180 R1N26A / D3030,
R1156-F0018-01, Questone_2 with BCM56873_A0:

- SONiC 202411-kamel.690: the program corrected QSFP LEDs that previously
  lit SFP indicators. Link movements involving QSFP1, QSFP2 and QSFP5 were
  visually checked. QSFP2 and QSFP5 were also statically split into 4x25G;
  all eight lane LEDs were confirmed with no SFP mislighting. The ports
  were then restored to 100G.
- SONiC test_rc4-candidate-kamel.77-837e6f735: after the first hardware
  boot, all 56 front-panel ports were present and QSFP2/Ethernet52 and
  QSFP5/Ethernet64 linked at 100G. The same program hash was verified and
  the operator visually confirmed the two QSFP indicators were correct,
  with no SFP indicators incorrectly lit. RC4 was not tested in 4x25G mode.

These checks do not establish every front-panel LED, every HPE hardware
revision, or dynamic breakout support. The on-device adaptation installed
the generated program directly; the upstream selector's migration and
preservation behavior is covered by isolated tests, not this hardware run.
