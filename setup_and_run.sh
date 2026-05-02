#!/bin/bash
# AI Exam Proctor Service - Quick Setup & Run Script
# Usage: ./setup_and_run.sh

set -e

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║     AI EXAM PROCTOR SERVICE — Setup & Run Script             ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check Python
echo -e "${BLUE}Checking Python...${NC}"
if ! command -v python3 &> /dev/null; then
    if ! command -v python &> /dev/null; then
        echo -e "${RED}✗ Python not found. Please install Python 3.8+${NC}"
        exit 1
    fi
    PYTHON=python
else
    PYTHON=python3
fi
echo -e "${GREEN}✓ Python found: $(${PYTHON} --version)${NC}"
echo

# Check Node.js (optional)
if command -v node &> /dev/null; then
    echo -e "${GREEN}✓ Node.js found: $(node --version)${NC}"
    HAS_NODE=true
else
    echo -e "${YELLOW}ℹ Node.js not found (optional for CLI client)${NC}"
    HAS_NODE=false
fi
echo

# Create virtual environment if not exists
if [ ! -d "venv" ]; then
    echo -e "${BLUE}Creating Python virtual environment...${NC}"
    ${PYTHON} -m venv venv
    echo -e "${GREEN}✓ Virtual environment created${NC}"
else
    echo -e "${GREEN}✓ Virtual environment exists${NC}"
fi
echo

# Activate virtual environment
echo -e "${BLUE}Activating virtual environment...${NC}"
source venv/bin/activate
echo -e "${GREEN}✓ Virtual environment activated${NC}"
echo

# Install Python dependencies
echo -e "${BLUE}Installing Python dependencies...${NC}"
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo -e "${GREEN}✓ Python dependencies installed${NC}"
echo

# Install Node dependencies if Node.js is available
if [ "$HAS_NODE" = true ]; then
    echo -e "${BLUE}Installing Node.js dependencies...${NC}"
    npm install -q --no-audit --no-fund
    echo -e "${GREEN}✓ Node.js dependencies installed${NC}"
    echo
fi

# Check if model file exists
if [ ! -f "face_landmarker.task" ]; then
    echo -e "${YELLOW}⚠ Warning: face_landmarker.task not found${NC}"
    echo -e "   Download it from: https://developers.google.com/mediapipe/solutions/vision/face_landmarker"
    echo
fi

# Display options
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}Setup complete! Choose how to run:${NC}"
echo
echo -e "${BLUE}1. Start the service:${NC}"
echo "   ${PYTHON} proctor_service.py"
echo
echo -e "${BLUE}2. Then in another terminal, use one of:${NC}"
echo
if [ "$HAS_NODE" = true ]; then
    echo -e "   • JavaScript CLI:"
    echo "     node proctor_client.js start"
    echo "     node proctor_client.js monitor"
    echo
fi
echo -e "   • Web Dashboard:"
echo "     Open http://localhost:5000 in your browser"
echo
echo -e "   • REST API:"
echo "     curl -X POST http://localhost:5000/api/start"
echo "     curl http://localhost:5000/api/status"
echo
echo -e "${BLUE}3. Documentation:${NC}"
echo "   • Quick Start:     QUICKSTART.md"
echo "   • API Reference:   SERVICE_API_GUIDE.md"
echo "   • Implementation:  IMPLEMENTATION_SUMMARY.md"
echo "   • File Manifest:   FILE_MANIFEST.md"
echo
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo
read -p "Would you like to start the service now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${BLUE}Starting AI Exam Proctor Service...${NC}"
    echo
    ${PYTHON} proctor_service.py
fi
