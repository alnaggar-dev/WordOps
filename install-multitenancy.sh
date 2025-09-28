#!/usr/bin/env bash
# =============================================================================
# WordOps Multi-tenancy Plugin Installation Script
# =============================================================================
# This script installs the WordPress multi-tenancy plugin for WordOps
# Run with: sudo bash install-multitenancy.sh
# =============================================================================

set -euo pipefail

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script configuration
SCRIPT_VERSION="1.0.0"
WORDOPS_MIN_VERSION="3.20.0"

# ----------------------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------------------
print_header() {
    echo -e "${BLUE}"
    echo "============================================================================="
    echo "         WordOps Multi-tenancy Plugin Installer v${SCRIPT_VERSION}"
    echo "============================================================================="
    echo -e "${NC}"
}

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

# ----------------------------------------------------------------------------
# Prerequisite Checks
# ----------------------------------------------------------------------------
check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "This script must be run with sudo"
        echo "Usage: sudo bash install-multitenancy.sh"
        exit 1
    fi
}

check_wordops() {
    if ! command -v wo >/dev/null 2>&1; then
        print_error "WordOps is not installed"
        echo "Please install WordOps first: https://wordops.net/docs/getting-started/installation-guide/"
        exit 1
    fi
    
    # Check WordOps version
    local wo_version=$(wo --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -n1)
    print_info "WordOps version detected: ${wo_version}"
}

check_dependencies() {
    local missing=()
    
    for cmd in wp curl unzip rsync jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        print_warn "Missing dependencies: ${missing[*]}"
        print_info "Installing missing dependencies..."
        apt-get update >/dev/null 2>&1
        apt-get install -y "${missing[@]}" >/dev/null 2>&1
    fi
    
    print_success "All dependencies are installed"
}

# ----------------------------------------------------------------------------
# Installation Functions
# ----------------------------------------------------------------------------
install_plugin_files() {
    print_info "Installing plugin files..."
    
    # Create plugin directories if they don't exist
    mkdir -p /var/lib/wo/plugins
    mkdir -p /etc/wo/plugins.d
    mkdir -p /var/lib/wo/templates
    
    # Check if plugin files exist in current directory
    local plugin_dir=""
    
    if [[ -f "wo/cli/plugins/multitenancy.py" ]]; then
        plugin_dir="."
    elif [[ -f "multitenancy.py" ]]; then
        plugin_dir="."
    else
        print_error "Plugin files not found in current directory"
        print_info "Please run this script from the WordOps repository root"
        exit 1
    fi
    
    # Copy plugin modules
    if [[ -f "${plugin_dir}/wo/cli/plugins/multitenancy.py" ]]; then
        cp "${plugin_dir}/wo/cli/plugins/multitenancy.py" /var/lib/wo/plugins/
        print_success "Installed multitenancy.py"
    fi
    
    if [[ -f "${plugin_dir}/wo/cli/plugins/multitenancy_functions.py" ]]; then
        cp "${plugin_dir}/wo/cli/plugins/multitenancy_functions.py" /var/lib/wo/plugins/
        print_success "Installed multitenancy_functions.py"
    fi
    
    if [[ -f "${plugin_dir}/wo/cli/plugins/multitenancy_db.py" ]]; then
        cp "${plugin_dir}/wo/cli/plugins/multitenancy_db.py" /var/lib/wo/plugins/
        print_success "Installed multitenancy_db.py"
    fi
    
    # Copy configuration file
    if [[ -f "${plugin_dir}/config/plugins.d/multitenancy.conf" ]]; then
        if [[ ! -f "/etc/wo/plugins.d/multitenancy.conf" ]]; then
            cp "${plugin_dir}/config/plugins.d/multitenancy.conf" /etc/wo/plugins.d/
            print_success "Installed configuration file"
        else
            print_warn "Configuration file already exists, skipping"
        fi
    fi
    
    # Set permissions
    chmod 644 /var/lib/wo/plugins/multitenancy*.py 2>/dev/null || true
    chmod 644 /etc/wo/plugins.d/multitenancy.conf 2>/dev/null || true
}

# Template creation removed - using WordOps native templates
# The multi-tenancy plugin uses WordOps' existing nginx templates
# which work perfectly with the shared WordPress setup

enable_plugin() {
    print_info "Enabling multi-tenancy plugin in WordOps..."
    
    # Add plugin to WordOps configuration if not already present
    local wo_config="/etc/wo/wo.conf"
    
    if [[ -f "$wo_config" ]]; then
        if ! grep -q "\[multitenancy\]" "$wo_config"; then
            echo "" >> "$wo_config"
            echo "[multitenancy]" >> "$wo_config"
            echo "enable_plugin = true" >> "$wo_config"
            print_success "Added plugin to WordOps configuration"
        else
            print_warn "Plugin already configured in WordOps"
        fi
    fi
}

verify_installation() {
    print_info "Verifying installation..."
    
    # Check if plugin files exist
    local files_ok=true
    for file in multitenancy.py multitenancy_functions.py multitenancy_db.py; do
        if [[ ! -f "/var/lib/wo/plugins/${file}" ]]; then
            print_error "Missing plugin file: ${file}"
            files_ok=false
        fi
    done
    
    if [[ "$files_ok" == "true" ]]; then
        print_success "All plugin files installed correctly"
    else
        print_error "Plugin installation incomplete"
        exit 1
    fi
    
    # Test if plugin loads
    print_info "Testing plugin loading..."
    if wo multitenancy --help >/dev/null 2>&1; then
        print_success "Plugin loaded successfully"
    else
        print_warn "Plugin may not be loaded yet. You may need to restart WordOps"
    fi
}

# ----------------------------------------------------------------------------
# Post-Installation
# ----------------------------------------------------------------------------
show_usage() {
    echo ""
    echo -e "${BLUE}=============================================================================${NC}"
    echo -e "${GREEN}Installation Complete!${NC}"
    echo -e "${BLUE}=============================================================================${NC}"
    echo ""
    echo "The WordOps Multi-tenancy plugin has been installed successfully."
    echo ""
    echo -e "${YELLOW}Next Steps:${NC}"
    echo ""
    echo "1. Initialize the shared WordPress infrastructure:"
    echo -e "   ${GREEN}sudo wo multitenancy init${NC}"
    echo ""
    echo "2. Create your first shared WordPress site:"
    echo -e "   ${GREEN}sudo wo multitenancy create example.com${NC}"
    echo ""
    echo "3. View available commands:"
    echo -e "   ${GREEN}sudo wo multitenancy --help${NC}"
    echo ""
    echo -e "${YELLOW}Configuration:${NC}"
    echo ""
    echo "Edit the configuration file to customize settings:"
    echo -e "   ${GREEN}sudo nano /etc/wo/plugins.d/multitenancy.conf${NC}"
    echo ""
    echo -e "${YELLOW}Documentation:${NC}"
    echo ""
    echo "For more information, see the documentation at:"
    echo "https://github.com/WordOps/WordOps/wiki/Multi-tenancy"
    echo ""
    echo -e "${BLUE}=============================================================================${NC}"
}

# ----------------------------------------------------------------------------
# Uninstall Function (for --uninstall flag)
# ----------------------------------------------------------------------------
uninstall_plugin() {
    print_warn "Uninstalling WordOps Multi-tenancy plugin..."
    
    read -p "This will remove the plugin but keep your shared sites. Continue? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Uninstall cancelled"
        exit 0
    fi
    
    # Remove plugin files
    rm -f /var/lib/wo/plugins/multitenancy*.py
    rm -f /var/lib/wo/templates/mt-*.mustache
    
    print_warn "Configuration file kept at: /etc/wo/plugins.d/multitenancy.conf"
    print_warn "Shared infrastructure kept at: /var/www/shared"
    
    print_success "Plugin uninstalled"
    echo "To completely remove all data, run:"
    echo "  sudo wo multitenancy remove --force"
    echo "  sudo rm -rf /var/www/shared"
    echo "  sudo rm -f /etc/wo/plugins.d/multitenancy.conf"
}

# ----------------------------------------------------------------------------
# Main Execution
# ----------------------------------------------------------------------------
main() {
    print_header
    
    # Check for uninstall flag
    if [[ "${1:-}" == "--uninstall" ]] || [[ "${1:-}" == "-u" ]]; then
        uninstall_plugin
        exit 0
    fi
    
    # Run installation steps
    print_info "Starting installation..."
    echo ""
    
    check_root
    check_wordops
    check_dependencies
    
    echo ""
    install_plugin_files
    # Templates not needed - using WordOps native templates
    enable_plugin
    
    echo ""
    verify_installation
    
    show_usage
}

# Run main function
main "$@"