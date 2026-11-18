"""Acoustic modem bench app package.

Layout (transport and protocol are kept separate from the widgets so the RX tab
can reuse them):

    tx_model.py   - TX hardware model, STATUS parsing, safe command ordering (no Qt)
    sim_tx.py     - pure-Python simulated TX Teensy (same protocol as tx_firmware.ino)
    links.py      - line-based device links: real QSerialPort and in-process simulator
    widgets.py    - reusable widgets (port bar, spin+slider)
    tx_tab.py     - TX tab
    rx_tab.py     - RX tab placeholder
    main_window.py
"""
