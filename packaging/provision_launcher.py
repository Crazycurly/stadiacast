"""Entry point of the frozen ``stadiacast-provision`` executable: Wi-Fi provisioning over Bluetooth."""

import sys

from stadiacast.ble_provisioner import main

if __name__ == "__main__":
    sys.exit(main())
