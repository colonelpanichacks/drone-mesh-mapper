#!/usr/bin/env python3
"""
Mesh-Mapper Setup Script
Downloads mesh-mapper.py from colonelpanichacks/drone-mesh-mapper and sets up auto-start cron job.

Usage:
    python3 setup_mesh_mapper.py --branch main
    python3 setup_mesh_mapper.py --branch dev
    python3 setup_mesh_mapper.py --branch Dev --install-dir /custom/path
"""

import os
import sys
import argparse
import subprocess
import requests
import getpass
from pathlib import Path
from urllib.parse import urlparse

# GitHub repository configuration
GITHUB_REPO = "colonelpanichacks/drone-mesh-mapper"
GITHUB_BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}"
TARGET_FILE = "mesh-mapper.py"

def get_current_user():
    """Get the current username"""
    return getpass.getuser()

def get_user_home():
    """Get the current user's home directory"""
    return str(Path.home())

def construct_download_url(branch):
    """Construct the raw GitHub URL for downloading the file"""
    return f"{GITHUB_BASE_URL}/{branch}/{TARGET_FILE}"

def download_file(url, destination):
    """Download a file from URL to destination"""
    print(f"📥 Downloading from: {url}")
    print(f"📁 Saving to: {destination}")
    
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        
        with open(destination, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        # Make the file executable
        os.chmod(destination, 0o755)
        
        print(f"✅ Downloaded successfully: {destination}")
        return True
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error downloading file: {e}")
        return False
    except Exception as e:
        print(f"❌ Error saving file: {e}")
        return False

def check_file_exists(url):
    """Check if file exists at the URL"""
    try:
        response = requests.head(url, timeout=10)
        return response.status_code == 200
    except:
        return False

def get_current_crontab():
    """Get current user's crontab"""
    try:
        result = subprocess.run(['crontab', '-l'], 
                              capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return ""  # No crontab exists
    except Exception as e:
        print(f"⚠️  Error reading crontab: {e}")
        return ""

def install_cron_job(install_dir):
    """Install the cron job for auto-start"""
    current_user = get_current_user()
    user_home = get_user_home()
    
    # Construct the cron job command
    cron_command = f"@reboot sleep 5 && cd {install_dir} && /usr/bin/python3 {TARGET_FILE} --debug"
    
    print(f"🔧 Setting up cron job for user: {current_user}")
    print(f"📍 Install directory: {install_dir}")
    print(f"⚙️  Cron command: {cron_command}")
    
    # Get current crontab
    current_crontab = get_current_crontab()
    
    # Check if our cron job already exists
    if f"cd {install_dir} && /usr/bin/python3 {TARGET_FILE}" in current_crontab:
        print("ℹ️  Cron job already exists, updating...")
        # Remove old entries
        lines = current_crontab.split('\n')
        filtered_lines = [line for line in lines 
                         if not (f"cd {install_dir}" in line and TARGET_FILE in line)]
        current_crontab = '\n'.join(filtered_lines).strip()
    
    # Add our cron job
    if current_crontab:
        new_crontab = current_crontab + '\n' + cron_command
    else:
        new_crontab = cron_command
    
    try:
        # Install the new crontab
        process = subprocess.Popen(['crontab', '-'], 
                                 stdin=subprocess.PIPE, 
                                 stdout=subprocess.PIPE, 
                                 stderr=subprocess.PIPE,
                                 text=True)
        stdout, stderr = process.communicate(new_crontab)
        
        if process.returncode == 0:
            print("✅ Cron job installed successfully!")
            print("🔄 The mesh-mapper will auto-start on system reboot")
            return True
        else:
            print(f"❌ Error installing cron job: {stderr}")
            return False
            
    except Exception as e:
        print(f"❌ Error setting up cron job: {e}")
        return False

def verify_installation(install_path, cron_expected=True):
    """Verify the installation"""
    print("\n🔍 Verifying installation...")
    
    # Check if file exists and is executable
    if not os.path.exists(install_path):
        print(f"❌ File not found: {install_path}")
        return False
    
    if not os.access(install_path, os.X_OK):
        print(f"⚠️  File is not executable: {install_path}")
        return False
    
    # Check file size (should be substantial)
    file_size = os.path.getsize(install_path)
    if file_size < 1000:  # Less than 1KB is suspicious
        print(f"⚠️  File seems too small ({file_size} bytes): {install_path}")
        return False
    
    print(f"✅ Installation verified: {install_path} ({file_size} bytes)")
    
    # Verify cron job only if expected
    if cron_expected:
        current_crontab = get_current_crontab()
        if TARGET_FILE in current_crontab and "@reboot" in current_crontab:
            print("✅ Cron job verified")
            return True
        else:
            print("⚠️  Cron job not found in crontab")
            return False
    else:
        print("ℹ️  Cron job verification skipped (--no-cron used)")
        return True

def main():
    parser = argparse.ArgumentParser(
        description="Download and setup mesh-mapper.py from GitHub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 setup_mesh_mapper.py --branch main
  python3 setup_mesh_mapper.py --branch Dev
  python3 setup_mesh_mapper.py --branch main --install-dir /opt/mesh-mapper
  python3 setup_mesh_mapper.py --branch Dev --no-cron
        """)
    
    parser.add_argument('--branch', 
                       choices=['main', 'Dev', 'dev'], 
                       default='main',
                       help='GitHub branch to download from (default: main)')
    
    parser.add_argument('--install-dir', 
                       default=None,
                       help='Installation directory (default: ~/mesh-mapper)')
    
    parser.add_argument('--no-cron', 
                       action='store_true',
                       help='Skip installing cron job')
    
    parser.add_argument('--force', 
                       action='store_true',
                       help='Force overwrite existing files')
    
    args = parser.parse_args()
    
    # Normalize branch name (GitHub is case-sensitive)
    if args.branch.lower() == 'dev':
        branch = 'Dev'  # GitHub uses capital D
    else:
        branch = args.branch
    
    # Determine installation directory
    if args.install_dir:
        install_dir = os.path.abspath(args.install_dir)
    else:
        install_dir = os.path.join(get_user_home(), 'mesh-mapper')
    
    install_path = os.path.join(install_dir, TARGET_FILE)
    
    print("=" * 60)
    print("🚁 MESH-MAPPER GITHUB SETUP SCRIPT")
    print("=" * 60)
    print(f"📦 Repository: https://github.com/{GITHUB_REPO}")
    print(f"🌿 Branch: {branch}")
    print(f"👤 User: {get_current_user()}")
    print(f"🏠 Home: {get_user_home()}")
    print(f"📁 Install Dir: {install_dir}")
    print(f"📄 Target File: {TARGET_FILE}")
    print()
    
    # Check if file already exists
    if os.path.exists(install_path) and not args.force:
        response = input(f"File already exists: {install_path}\nOverwrite? (y/N): ")
        if response.lower() != 'y':
            print("❌ Installation cancelled")
            return 1
    
    # Construct download URL
    download_url = construct_download_url(branch)
    
    # Check if file exists on GitHub
    print("🔍 Checking if file exists on GitHub...")
    if not check_file_exists(download_url):
        print(f"❌ File not found at: {download_url}")
        print(f"   Please check if the branch '{branch}' exists and contains {TARGET_FILE}")
        return 1
    
    print(f"✅ File found on GitHub: {branch}/{TARGET_FILE}")
    
    # Download the file
    if not download_file(download_url, install_path):
        print("❌ Download failed")
        return 1
    
    # Install cron job (if requested)
    if not args.no_cron:
        print("\n🕒 Setting up auto-start cron job...")
        if not install_cron_job(install_dir):
            print("⚠️  Cron job installation failed, but file was downloaded successfully")
    else:
        print("⏭️  Skipping cron job installation (--no-cron specified)")
    
    # Verify installation
    if verify_installation(install_path, not args.no_cron):
        print("\n🎉 Installation completed successfully!")
        print(f"📍 Mesh-mapper installed at: {install_path}")
        
        if not args.no_cron:
            print("🔄 Auto-start enabled: Will run on system reboot")
            print("💡 To test manually, run:")
            print(f"   cd {install_dir} && python3 {TARGET_FILE} --debug")
        
        print("\n📋 Next steps:")
        print("  1. Connect your ESP32 device")
        print("  2. Run the mesh-mapper manually to test")
        print("  3. Reboot to test auto-start (if cron enabled)")
        print(f"  4. Access web interface at: http://localhost:5000")
        
        return 0
    else:
        print("❌ Installation verification failed")
        return 1

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n⏹️  Installation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        sys.exit(1) 
