#!/usr/bin/env python3
"""
Cron Installer for mesh-mapper.py
Installs a cron job to auto-start mesh-mapper.py on system reboot.

Usage:
    python3 cron.py
    python3 cron.py --path /path/to/drone-mapper
"""

import os
import sys
import argparse
import subprocess
from pathlib import Path

def get_current_user():
    """Get the current username"""
    import getpass
    return getpass.getuser()

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
        print(f"Error reading crontab: {e}")
        return ""

def install_cron_job(mapper_path):
    """Install the cron job for auto-start"""
    if not os.path.exists(mapper_path):
        print(f"Error: mesh-mapper.py not found at {mapper_path}")
        return False
    
    mapper_dir = os.path.dirname(os.path.abspath(mapper_path))
    mapper_file = os.path.basename(mapper_path)
    
    # Construct the cron job command
    cron_command = f"@reboot sleep 5 && cd {mapper_dir} && /usr/bin/python3 {mapper_file} --debug"
    
    print(f"Setting up cron job for: {mapper_path}")
    print(f"Cron command: {cron_command}")
    
    # Get current crontab
    current_crontab = get_current_crontab()
    
    # Check if our cron job already exists
    if mapper_file in current_crontab and "@reboot" in current_crontab:
        print("Cron job already exists, updating...")
        # Remove old entries
        lines = current_crontab.split('\n')
        filtered_lines = [line for line in lines 
                         if not (mapper_file in line and "@reboot" in line)]
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
            print("Cron job installed successfully!")
            print("The mesh-mapper will auto-start on system reboot")
            return True
        else:
            print(f"Error installing cron job: {stderr}")
            return False
            
    except Exception as e:
        print(f"Error setting up cron job: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(
        description="Install cron job to auto-start mesh-mapper.py on reboot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 cron.py
  python3 cron.py --path /opt/mesh-mapper/drone-mapper/mesh-mapper.py
        """)
    
    parser.add_argument('--path', 
                       default=None,
                       help='Path to mesh-mapper.py (default: ../drone-mapper/mesh-mapper.py)')
    
    args = parser.parse_args()
    
    # Determine mapper path
    if args.path:
        mapper_path = os.path.abspath(args.path)
    else:
        # Default: mesh-mapper.py is in parent directory (drone-mapper folder)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        mapper_path = os.path.join(os.path.dirname(script_dir), 'mesh-mapper.py')
    
    print("=" * 60)
    print("MESH-MAPPER CRON INSTALLER")
    print("=" * 60)
    print(f"User: {get_current_user()}")
    print(f"Mapper path: {mapper_path}")
    print()
    
    # Verify file exists
    if not os.path.exists(mapper_path):
        print(f"Error: mesh-mapper.py not found at {mapper_path}")
        print("Please specify the correct path with --path option")
        return 1
    
    # Install cron job
    if install_cron_job(mapper_path):
        print("\nInstallation completed successfully!")
        print(f"Mesh-mapper will auto-start from: {mapper_path}")
        print("To test manually, run:")
        print(f"  cd {os.path.dirname(mapper_path)} && python3 mesh-mapper.py --debug")
        return 0
    else:
        print("Installation failed")
        return 1

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInstallation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)
