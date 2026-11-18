"""Acoustic modem bench app package.

Transport and protocol are kept separate from the widgets:

    tx_model.py     - TX hardware model, STATUS parsing, safe command ordering (no Qt)
    sim_tx.py       - pure-Python simulated TX Teensy (same protocol as tx_firmware.ino)
    links.py        - line-based device links: real QSerialPort and in-process simulator
    rx_protocol.py  - RX byte stream: frame format, CRC, incremental parser (no Qt)
    rx_model.py     - RX gain model, hydrophone -> ADC (no Qt)
    rx_analysis.py  - RX measurements, spectra, delay estimation (no Qt)
    rx_io.py        - save / load RX captures (.npy + .json, .csv)
    sim_rx.py       - pure-Python fake RX Teensy (same protocol as rx_firmware.ino)
    rx_link.py      - frame-aware RX links: real QSerialPort and in-process simulator
    widgets.py      - reusable widgets (port bar, spin+slider)
    tx_tab.py       - TX tab
    rx_tab.py       - RX tab
    main_window.py
"""
