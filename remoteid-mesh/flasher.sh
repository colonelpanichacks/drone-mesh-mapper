#!/bin/bash
set -e  # Exit immediately if a command exits with a non-zero status

# Variables
ESPTOOL_REPO="https://github.com/espressif/esptool"
FIRMWARE_URL="https://raw.githubusercontent.com/colonelpanichacks/wifi-rid-to-mesh/main/remoteid-mesh/firmware.bin"
ESPTOOL_DIR="esptool"

# PlatformIO Config Values
MONITOR_SPEED=115200
UPLOAD_SPEED=115200
ESP32_PORT=""

# Function to find serial devices
find_serial_devices() {
    local devices=""

    # Linux devices
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Try physical devices first
        devices=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true)

        # If no devices found, try by-id paths
        if [ -z "$devices" ] && [ -d "/dev/serial/by-id" ]; then
            devices=$(ls /dev/serial/by-id/* 2>/dev/null || true)
        fi

        # If still no devices, try by-path
        if [ -z "$devices" ] && [ -d "/dev/serial/by-path" ]; then
            devices=$(ls /dev/serial/by-path/* 2>/dev/null || true)
        fi
    # macOS devices
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        # On macOS, prefer /dev/cu.* over /dev/tty.* as they work better for flashing
        devices=$(ls /dev/cu.* 2>/dev/null | grep -i -E 'usb|serial|usbmodem' || true)
    fi

    echo "$devices"
}

# Function to stop services if on Linux
stop_services() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo "Checking for running services that might interfere..."
        if command -v systemctl &> /dev/null; then
            if systemctl is-active --quiet zmq-decoder.service; then
                echo "Stopping zmq-decoder service..."
                sudo systemctl stop zmq-decoder.service
            else
                echo "zmq-decoder.service is not running."
            fi
        else
            echo "systemctl not found. Skipping service management."
        fi
    fi
}

# Clear screen for better UX
clear

# --- Place your ASCII art here ---
cat << "EOF"


   _____                .__              ________          __                 __   
  /     \   ____   _____|  |__           \______ \   _____/  |_  ____   _____/  |_ 
 /  \ /  \_/ __ \ /  ___/  |  \   ______  |    |  \_/ __ \   __\/ __ \_/ ___\   __\
/    Y    \  ___/ \___ \|   Y  \ /_____/  |    `   \  ___/|  | \  ___/\  \___|  |  
\____|__  /\___  >____  >___|  /         /_______  /\___  >__|  \___  >\___  >__|  
        \/     \/     \/     \/                  \/     \/          \/     \/      
                ___________.__                .__                                  
                \_   _____/|  | _____    _____|  |__   ___________                 
                 |    __)  |  | \__  \  /  ___/  |  \_/ __ \_  __ \                
                 |     \   |  |__/ __ \_\___ \|   Y  \  ___/|  | \/                
                 \___  /   |____(____  /____  >___|  /\___  >__|                   
                     \/              \/     \/     \/     \/                       
                                                                                   
                                                                                   
EOF

echo "==================================================="
echo "Mesh-Detect Remote Id Firmware Flasher Tool"
echo "==================================================="

# Clone the esptool repository if it doesn't already exist
if [ ! -d "$ESPTOOL_DIR" ]; then
    echo "Cloning esptool repository..."
    git clone "$ESPTOOL_REPO"
else
    echo "Directory '$ESPTOOL_DIR' already exists."
fi

# Change to the esptool directory
cd "$ESPTOOL_DIR"

# Download firmware automatically
FIRMWARE_FILE=$(basename "$FIRMWARE_URL")
echo ""
echo "Downloading mesh detect remote id firmware..."
wget "$FIRMWARE_URL" -O "$FIRMWARE_FILE"

# Find available USB serial devices
echo ""
echo "Searching for USB serial devices..."
serial_devices=$(find_serial_devices)

if [ -z "$serial_devices" ]; then
    echo "ERROR: No USB serial devices found."
    echo "Please check your connection and try again."
    exit 1
fi

# Display serial devices and let user select one
echo ""
echo "==================================================="
echo "Found USB serial devices:"
echo "==================================================="
select device in $serial_devices; do
    if [ -n "$device" ]; then
        ESP32_PORT="$device"
        echo ""
        echo "Selected USB serial device: $ESP32_PORT"
        break
    else
        echo "Invalid selection. Please try again."
    fi
done

# Stop any interfering services
stop_services

# Flash the firmware using esptool.py for the ESP32-C3
echo ""
echo "Flashing drone firmware to the device..."
python3 esptool.py \
    --chip esp32c3 \
    --port "$ESP32_PORT" \
    --baud "$UPLOAD_SPEED" \
    --before default_reset \
    --after hard_reset \
    write_flash -z \
    --flash_mode dio \
    --flash_freq 80m \
    --flash_size 4MB \
    0x10000 "$FIRMWARE_FILE"

echo ""
echo "==================================================="
echo "Firmware flashing complete!"
echo "==================================================="
