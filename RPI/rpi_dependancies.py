#!/usr/bin/env python3
"""
Universal system-wide dependency installer for mapper.py
Works on different systems (Linux, macOS, Windows)
Enhanced version with comprehensive dependency detection and installation
"""

import subprocess
import sys
import os
import platform

def print_banner():
    """Print a nice banner"""
    print("🚀" + "="*60 + "🚀")
    print("   UNIVERSAL MAPPER.PY DEPENDENCY INSTALLER")
    print("🚀" + "="*60 + "🚀")

def detect_system():
    """Detect the operating system and package manager"""
    system = platform.system().lower()
    print(f"🔍 Detected system: {platform.system()} {platform.release()}")
    
    # Detect package manager
    package_managers = {
        'apt': 'sudo apt update && sudo apt install -y python3-pip',
        'yum': 'sudo yum install -y python3-pip',
        'dnf': 'sudo dnf install -y python3-pip',
        'pacman': 'sudo pacman -S python-pip',
        'brew': 'brew install python3',
        'pkg': 'sudo pkg install py39-pip',  # FreeBSD
    }
    
    available_pm = None
    for pm in package_managers.keys():
        if run_command_silent(f"which {pm}")[0]:
            available_pm = pm
            print(f"✅ Found package manager: {pm}")
            break
    
    return system, available_pm

def run_command_silent(cmd):
    """Run a command silently and return success status"""
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr

def run_command(cmd):
    """Run a command and return success status with output"""
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        print(f"✅ {cmd}")
        if result.stdout.strip():
            # Only print first few lines to avoid spam
            lines = result.stdout.strip().split('\n')
            if len(lines) > 3:
                print(f"   Output: {lines[0]}...{lines[-1]}")
            else:
                print(f"   Output: {result.stdout.strip()}")
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        print(f"❌ {cmd}")
        if e.stderr:
            print(f"   Error: {e.stderr.strip()}")
        return False, e.stderr

def check_python():
    """Check Python version compatibility"""
    print("\n🐍 Checking Python installation...")
    
    python_commands = ["python3", "python"]
    for py_cmd in python_commands:
        success, output = run_command_silent(f"{py_cmd} --version")
        if success:
            version_info = output.strip()
            print(f"✅ Found: {py_cmd} - {version_info}")
            
            # Check if version is compatible (3.7+)
            try:
                version_str = version_info.split()[1]
                major, minor = map(int, version_str.split('.')[:2])
                if major >= 3 and minor >= 7:
                    print(f"✅ Python version is compatible")
                    return py_cmd
                else:
                    print(f"⚠️  Python {major}.{minor} found, but 3.7+ recommended")
                    return py_cmd  # Still try to use it
            except:
                return py_cmd  # If we can't parse version, still try
    
    print("❌ No Python installation found!")
    return None

def check_pip():
    """Check which pip command is available"""
    pip_commands = [
        "pip3",
        "pip",
        "python3 -m pip",
        "python -m pip"
    ]
    
    print("\n🔍 Checking for pip installation...")
    
    for pip_cmd in pip_commands:
        success, output = run_command_silent(f"{pip_cmd} --version")
        if success:
            print(f"✅ Found: {pip_cmd} - {output.strip()}")
            return pip_cmd
    
    print("❌ No pip found!")
    return None

def install_pip(system, package_manager):
    """Try to install pip if not found"""
    print("\n🔧 Attempting to install pip...")
    
    # System-specific installation methods
    install_methods = []
    
    if package_manager:
        if package_manager == 'apt':
            install_methods.extend([
                "sudo apt update && sudo apt install -y python3-pip python3-venv python3-dev",
                "sudo apt install -y python3-pip"
            ])
        elif package_manager == 'yum':
            install_methods.extend([
                "sudo yum install -y python3-pip python3-devel",
                "sudo yum install -y python3-pip"
            ])
        elif package_manager == 'dnf':
            install_methods.extend([
                "sudo dnf install -y python3-pip python3-devel",
                "sudo dnf install -y python3-pip"
            ])
        elif package_manager == 'pacman':
            install_methods.append("sudo pacman -S python-pip")
        elif package_manager == 'brew':
            install_methods.append("brew install python3")
        elif package_manager == 'pkg':
            install_methods.append("sudo pkg install py39-pip")
    
    # Universal methods
    install_methods.extend([
        "curl https://bootstrap.pypa.io/get-pip.py | python3",
        "wget -O - https://bootstrap.pypa.io/get-pip.py | python3",
        "python3 -m ensurepip --upgrade"
    ])
    
    for method in install_methods:
        print(f"🔧 Trying: {method}")
        success, output = run_command(method)
        if success:
            print("✅ pip installation successful!")
            return check_pip()  # Check again after installation
        
    print("❌ Could not install pip automatically")
    return None

def get_all_dependencies():
    """Get all required dependencies for mapper.py"""
    # Core dependencies from analyzing mapper.py
    dependencies = [
        "requests",           # HTTP library
        "urllib3",           # HTTP client library
        "pyserial",          # Serial communication
        "Flask",             # Web framework
        "flask-socketio",    # WebSocket support for Flask
        "Werkzeug",          # WSGI utility library
        "Jinja2",            # Template engine
        "click",             # Command line interface
        "itsdangerous",      # Cryptographic signing
        "MarkupSafe",        # String handling library
        "wheel",             # Built-package format
        "setuptools",        # Package development tools
        "eventlet",          # Async networking library (for socketio)
        "python-socketio",   # Socket.IO client/server
    ]
    
    # Optional but helpful packages
    optional_dependencies = [
        "pip-tools",         # Dependency management
        "psutil",           # System utilities
        "colorama",         # Cross-platform colored terminal text
    ]
    
    return dependencies, optional_dependencies

def install_dependencies(pip_cmd, system):
    """Install all required dependencies"""
    print("\n📦 Installing dependencies...")
    
    dependencies, optional_deps = get_all_dependencies()
    all_packages = dependencies + optional_deps
    
    print(f"Core dependencies: {len(dependencies)}")
    print(f"Optional dependencies: {len(optional_deps)}")
    print(f"Total packages: {len(all_packages)}")
    
    # Try different installation strategies
    strategies = [
        ("break-system-packages", f"{pip_cmd} install --break-system-packages"),
        ("sudo break-system-packages", f"sudo {pip_cmd} install --break-system-packages"),
        ("user install", f"{pip_cmd} install --user"),
        ("sudo install", f"sudo {pip_cmd} install"),
        ("force reinstall", f"{pip_cmd} install --force-reinstall"),
        ("basic install", f"{pip_cmd} install"),
    ]
    
    # Try installing all packages at once first
    packages_str = " ".join(all_packages)
    
    for strategy_name, base_cmd in strategies:
        print(f"\n🔧 Trying strategy: {strategy_name}")
        install_cmd = f"{base_cmd} {packages_str}"
        print(f"   Command: {install_cmd}")
        
        success, output = run_command(install_cmd)
        if success:
            print(f"\n🎉 All dependencies installed successfully using {strategy_name}!")
            return True, "all"
    
    # If batch installation failed, try installing core packages individually
    print("\n⚠️  Batch installation failed, trying individual package installation...")
    
    failed_packages = []
    successful_packages = []
    
    for package in dependencies:  # Only try core dependencies individually
        package_installed = False
        
        for strategy_name, base_cmd in strategies[:3]:  # Try top 3 strategies
            install_cmd = f"{base_cmd} {package}"
            print(f"🔧 Installing {package} with {strategy_name}...")
            
            success, output = run_command(install_cmd)
            if success:
                successful_packages.append(package)
                package_installed = True
                break
        
        if not package_installed:
            failed_packages.append(package)
    
    print(f"\n📊 Installation Summary:")
    print(f"✅ Successful: {len(successful_packages)} packages")
    if successful_packages:
        print(f"   {', '.join(successful_packages)}")
    
    if failed_packages:
        print(f"❌ Failed: {len(failed_packages)} packages")
        print(f"   {', '.join(failed_packages)}")
        return False, "partial"
    else:
        print(f"🎉 All core dependencies installed successfully!")
        return True, "individual"

def test_imports():
    """Test if all critical imports work"""
    print("\n🧪 Testing imports...")
    
    critical_imports = [
        ("os", "import os"),
        ("requests", "import requests"),
        ("serial", "import serial; import serial.tools.list_ports"),
        ("flask", "from flask import Flask"),
        ("flask_socketio", "from flask_socketio import SocketIO"),
    ]
    
    failed_imports = []
    
    for name, import_statement in critical_imports:
        try:
            exec(import_statement)
            print(f"✅ {name}")
        except ImportError as e:
            print(f"❌ {name} - {e}")
            failed_imports.append(name)
        except Exception as e:
            print(f"⚠️  {name} - {e}")
    
    if failed_imports:
        print(f"\n❌ Some imports failed: {', '.join(failed_imports)}")
        return False
    else:
        print(f"\n✅ All critical imports successful!")
        return True

def create_test_script():
    """Create a test script to verify installation"""
    test_script = """#!/usr/bin/env python3
# Quick test script for mapper.py dependencies

try:
    import os, time, json, csv, logging, threading
    import requests, urllib3, serial
    from flask import Flask
    from flask_socketio import SocketIO
    print("✅ All critical imports successful!")
    
    # Test serial port detection
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    print(f"✅ Serial port detection working ({len(ports)} ports found)")
    
    # Test Flask + SocketIO
    app = Flask(__name__)
    socketio = SocketIO(app)
    print("✅ Flask + SocketIO working")
    
    print("🎉 mapper.py dependencies are ready!")
    
except ImportError as e:
    print(f"❌ Import error: {e}")
except Exception as e:
    print(f"❌ Error: {e}")
"""
    
    try:
        with open("test_dependencies.py", "w") as f:
            f.write(test_script)
        print("✅ Created test_dependencies.py")
        return True
    except Exception as e:
        print(f"❌ Failed to create test script: {e}")
        return False

def print_final_summary(success, method, system):
    """Print final summary and next steps"""
    print("\n" + "🎯" + "="*60 + "🎯")
    print("                INSTALLATION SUMMARY")
    print("🎯" + "="*60 + "🎯")
    
    if success:
        print("✅ INSTALLATION SUCCESSFUL!")
        print(f"📦 Method used: {method}")
        print(f"💻 System: {system}")
        
        print(f"\n🚀 NEXT STEPS:")
        print(f"   1. Test your setup: python3 test_dependencies.py")
        print(f"   2. Run your mapper: python3 mapper.py")
        print(f"   3. For headless mode: python3 mapper.py --headless")
        print(f"   4. For help: python3 mapper.py --help")
        
    else:
        print("❌ INSTALLATION INCOMPLETE")
        print(f"\n🔧 TROUBLESHOOTING:")
        print(f"   1. Try with sudo: sudo python3 install_universal.py")
        print(f"   2. Manual install: pip3 install requests flask pyserial flask-socketio")
        print(f"   3. Use virtual environment: python3 -m venv venv && source venv/bin/activate")
        print(f"   4. Check system package manager (apt, yum, dnf, etc.)")
    
    print("\n📝 FILES CREATED:")
    print("   • test_dependencies.py - Test script")
    print("   • install_universal.py - This installer")
    
    print("🎯" + "="*60 + "🎯")

def main():
    """Main installation routine"""
    print_banner()
    
    # Detect system
    system, package_manager = detect_system()
    
    # Check Python
    python_cmd = check_python()
    if not python_cmd:
        print("\n❌ Python is required but not found!")
        return False
    
    # Check/install pip
    pip_cmd = check_pip()
    if not pip_cmd:
        pip_cmd = install_pip(system, package_manager)
        
    if not pip_cmd:
        print("\n❌ Cannot proceed without pip!")
        print("\n🔧 Manual pip installation:")
        if package_manager == 'apt':
            print("   sudo apt update && sudo apt install python3-pip")
        elif package_manager == 'yum':
            print("   sudo yum install python3-pip")
        elif package_manager == 'dnf':
            print("   sudo dnf install python3-pip")
        else:
            print("   curl https://bootstrap.pypa.io/get-pip.py | python3")
        return False
    
    # Install dependencies
    success, method = install_dependencies(pip_cmd, system)
    
    # Test imports
    import_success = test_imports()
    
    # Create test script
    create_test_script()
    
    # Final summary
    overall_success = success and import_success
    print_final_summary(overall_success, method, system)
    
    return overall_success

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Installation interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}")
        sys.exit(1) 