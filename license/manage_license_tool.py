"""
License Management Tool - Interactive & CLI Mode

Comprehensive tool for managing application licenses.
Supports both interactive mode and command-line arguments.

Usage:
    python manage_license_tool.py                    # Interactive mode
    python manage_license_tool.py --check            # CLI mode
    python manage_license_tool.py --create --days 30 # CLI mode
"""

# Show loading message immediately before any heavy imports
print("🔄 Initializing License Manager...")

import sys
import argparse
from pathlib import Path
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from license.license_manager import LicenseManager


# ============================================================================
# DISPLAY UTILITIES
# ============================================================================

def print_header(title):
    """Print formatted header."""
    print("\n" + "=" * 79)
    print(title.center(79))
    print("=" * 79)


def print_section(title):
    """Print formatted section."""
    print("\n" + "-" * 79)
    print(title)
    print("-" * 79)


def show_startup_banner():
    """Display application startup banner"""
    print("=" * 79)
    print("                    LICENSE MANAGEMENT UTILITY — PROCUCEV")
    print("=" * 79)
    print(f"Version : 1.0")
    print(f"Build   : {datetime.now().strftime('%Y%m%d')}")
    print("Status  : ✔ License Manager initialized successfully")
    print("=" * 79)
    print()
    print("Available Commands:")
    print("  • check      → Check current license status")
    print("  • create     → Create new license (with custom days)")
    print("  • renew      → Renew existing license (add days)")
    print("  • info       → Display detailed license information")
    print("  • help       → Display command reference")
    print("  • exit       → Close the utility")
    print("=" * 79)


def show_help():
    """Display detailed help information"""
    print("\nLICENSE MANAGEMENT UTILITY — HELP & DOCUMENTATION")
    print("=" * 79)
    print()
    print("COMMANDS")
    print("-" * 79)
    print("  check      → Validates current license and shows expiry status")
    print("  create     → Creates a new license (prompts for validity days)")
    print("  renew      → Extends existing license (prompts for additional days)")
    print("  info       → Shows comprehensive license details")
    print("  help       → Displays this help documentation")
    print("  exit       → Safely terminates the application")
    print()
    print("LICENSE STORAGE")
    print("-" * 79)
    print("License files are stored at:")
    print(f"  → {project_root / 'app' / 'license' / 'license.lic'}")
    print()
    print("LICENSE FORMAT")
    print("-" * 79)
    print("  • Encrypted using SHA-256 with complex secret key")
    print("  • Signature-based integrity verification")
    print("  • Machine ID binding (optional)")
    print()
    print("USAGE EXAMPLES")
    print("-" * 79)
    print("  Interactive Mode:")
    print("    python manage_license_tool.py")
    print()
    print("  Command-Line Mode:")
    print("    python manage_license_tool.py --check")
    print("    python manage_license_tool.py --create --days 30")
    print("    python manage_license_tool.py --renew --days 60")
    print("    python manage_license_tool.py --info")
    print("=" * 79)


# ============================================================================
# LICENSE OPERATIONS
# ============================================================================

def check_license(manager):
    """Check and display license status"""
    print_header("LICENSE STATUS CHECK")
    
    # Read license data
    license_data = manager.read_license_data()
    
    if not license_data:
        print_section("VALIDATION RESULT")
        print(f"✘ Status: INVALID")
        print(f"  License file not found or corrupted")
        print("=" * 79)
        return False
    
    # Check expiry
    expiry_date = datetime.fromisoformat(license_data["expires_at"])
    now = datetime.now()
    days_remaining = (expiry_date - now).days
    is_valid = now <= expiry_date
    
    print_section("VALIDATION RESULT")
    if is_valid:
        print(f"✔ Status: VALID")
        if days_remaining <= 7:
            print(f"  ⚠️  License expires soon in {days_remaining} days")
        else:
            print(f"  License expires in {days_remaining} days")
    else:
        days_expired = (now - expiry_date).days
        print(f"✘ Status: EXPIRED")
        print(f"  License expired {days_expired} days ago")
    
    info = manager.get_license_info()
    if info:
        print_section("LICENSE DETAILS")
        print(f"  Created At      : {info['created_at']}")
        print(f"  Expires At      : {info['expires_at']}")
        print(f"  Days Remaining  : {info['days_remaining']}")
        print(f"  Is Expired      : {info['is_expired']}")
        print(f"  Validity Period : {info['validity_days']} days")
        print(f"  Version         : {info['version']}")
        print(f"  Machine ID      : {info['machine_id']}")
    
    print("=" * 79)
    return is_valid


def create_license(manager, days=None):
    """Create new license"""
    print_header("CREATE NEW LICENSE")
    
    # Check if license already exists
    if manager.license_file.exists():
        print("\n⚠️  WARNING: A license file already exists!")
        info = manager.get_license_info()
        if info:
            print(f"   Current license expires: {info['expires_at']}")
            print(f"   Days remaining: {info['days_remaining']}")
        
        response = input("\n   Do you want to overwrite it? (yes/no): ").strip().lower()
        if response != 'yes':
            print("\n✘ License creation cancelled.")
            print("=" * 79)
            return False
    
    # Get validity days from user if not provided
    if days is None:
        while True:
            try:
                days_input = input("\nEnter validity days (default 30): ").strip()
                days = int(days_input) if days_input else 30
                
                if days <= 0:
                    print("⚠️  Days must be greater than 0. Please try again.")
                    continue
                
                break
            except ValueError:
                print("⚠️  Invalid input. Please enter a number.")
    
    print(f"\n🔄 Creating license with {days} days validity...")
    
    success = manager.create_license(days=days)
    
    if success:
        print(f"\n✔ License created successfully!")
        print(f"  Valid for {days} days")
        print(f"  Location: {manager.license_file}")
        
        # Show new license info
        info = manager.get_license_info()
        if info:
            print(f"\n  Expires on: {info['expires_at']}")
    else:
        print(f"\n✘ Failed to create license")
    
    print("=" * 79)
    return success


def renew_license(manager, days=None):
    """Renew existing license"""
    print_header("RENEW LICENSE")
    
    # Check if license exists
    if not manager.license_file.exists():
        print("\n✘ No license file found to renew")
        print("  Use 'create' command to create a new license first")
        print("=" * 79)
        return False
    
    # Show current status
    license_data = manager.read_license_data()
    if license_data:
        expiry_date = datetime.fromisoformat(license_data["expires_at"])
        now = datetime.now()
        days_remaining = (expiry_date - now).days
        is_valid = now <= expiry_date
        
        if is_valid:
            print(f"\nCurrent Status: Valid ({days_remaining} days remaining)")
        else:
            days_expired = (now - expiry_date).days
            print(f"\nCurrent Status: Expired ({days_expired} days ago)")
    else:
        print(f"\nCurrent Status: Invalid or corrupted license")
    
    info = manager.get_license_info()
    if info:
        print(f"Current Expiry: {info['expires_at']}")
    
    # Get additional days from user if not provided
    if days is None:
        while True:
            try:
                days_input = input("\nEnter days to add (default 30): ").strip()
                days = int(days_input) if days_input else 30
                
                if days <= 0:
                    print("⚠️  Days must be greater than 0. Please try again.")
                    continue
                
                break
            except ValueError:
                print("⚠️  Invalid input. Please enter a number.")
    
    print(f"\n🔄 Renewing license by adding {days} days...")
    
    success = manager.renew_license(additional_days=days)
    
    if success:
        print(f"\n✔ License renewed successfully!")
        print(f"  Added {days} days to expiry")
        
        # Show updated license info
        info = manager.get_license_info()
        if info:
            print(f"\n  New expiry date: {info['expires_at']}")
            print(f"  Days remaining: {info['days_remaining']}")
    else:
        print(f"\n✘ Failed to renew license")
    
    print("=" * 79)
    return success


def show_info(manager):
    """Display detailed license information"""
    print_header("LICENSE INFORMATION")
    
    print_section("LICENSE FILE")
    
    if manager.license_file.exists():
        print(f"  Location : {manager.license_file}")
        print(f"  Exists   : Yes")
        print(f"  Format   : Encrypted (SHA-256)")
        
        info = manager.get_license_info()
        if info:
            print()
            print(f"  Created    : {info['created_at']}")
            print(f"  Expires    : {info['expires_at']}")
            print(f"  Remaining  : {info['days_remaining']} days")
            print(f"  Status     : {'Expired' if info['is_expired'] else 'Active'}")
            print(f"  Version    : {info['version']}")
            print(f"  Machine ID : {info['machine_id']}")
        else:
            print()
            print("  ✘ License file corrupted or invalid")
    else:
        print(f"  Location : {manager.license_file}")
        print(f"  Exists   : No")
        print()
        print("  ⚠️  No license file found. Create one with 'create' command")
    
    print("=" * 79)


# ============================================================================
# INTERACTIVE MODE
# ============================================================================

def command_loop(manager):
    """Interactive command loop"""
    print("\n")
    
    while True:
        try:
            command = input("🔐 LICENSE> ").strip().lower()
            
            if command == "exit":
                print("👋 Exiting License Manager. Have a great day!")
                break
            
            elif command == "check":
                check_license(manager)
            
            elif command == "create":
                create_license(manager)
            
            elif command == "renew":
                renew_license(manager)
            
            elif command == "info":
                show_info(manager)
            
            elif command == "help":
                show_help()
            
            elif command == "":
                continue
            
            else:
                print(f"Unknown command: '{command}'. Type 'help' for available commands.")
                
        except KeyboardInterrupt:
            print("\n\n👋 Application terminated by user")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            import traceback
            traceback.print_exc()


# ============================================================================
# CLI MODE
# ============================================================================

def run_cli_mode(args, manager):
    """Run in command-line argument mode"""
    success = True
    
    if args.check:
        success = check_license(manager)
    elif args.create:
        success = create_license(manager, args.days)
    elif args.renew:
        success = renew_license(manager, args.days)
    elif args.info:
        show_info(manager)
    else:
        # No arguments provided, show help
        return None
    
    print("\n")
    return success


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main application entry point"""
    parser = argparse.ArgumentParser(
        description="License Management Tool for AI Procurement Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Interactive Mode:
    python manage_license_tool.py
  
  Command-Line Mode:
    python manage_license_tool.py --check              # Check license status
    python manage_license_tool.py --info               # Show detailed information
    python manage_license_tool.py --create --days 30   # Create 30-day license
    python manage_license_tool.py --renew --days 30    # Add 30 days to license
    python manage_license_tool.py --create --days 90   # Create 90-day license
        """
    )
    
    parser.add_argument("--check", action="store_true", 
                       help="Check license status")
    parser.add_argument("--create", action="store_true", 
                       help="Create new license")
    parser.add_argument("--renew", action="store_true", 
                       help="Renew existing license")
    parser.add_argument("--info", action="store_true", 
                       help="Show detailed license information")
    parser.add_argument("--days", type=int, default=30, 
                       help="Number of days (default: 30)")
    
    args = parser.parse_args()
    
    try:
        # Initialize license manager
        manager = LicenseManager()
        
        # Check if any CLI arguments provided
        has_cli_args = any([args.check, args.create, args.renew, args.info])
        
        if has_cli_args:
            # CLI Mode
            success = run_cli_mode(args, manager)
            if success is not None:
                sys.exit(0 if success else 1)
        else:
            # Interactive Mode
            show_startup_banner()
            command_loop(manager)
        
    except KeyboardInterrupt:
        print("\n\n👋 Application terminated by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Startup error: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)


if __name__ == '__main__':
    main()
