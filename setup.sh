#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  DELTA X · One-Command Setup
#  Usage: bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

echo "⚡ DELTA X · Setup Script"
echo "═══════════════════════════════════════════════════════════════════════════"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.10+"
    exit 1
fi
echo "✅ Python: $(python3 --version)"

# Create venv
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
echo "✅ Virtual environment activated"

# Install deps
echo "📥 Installing dependencies..."
pip install -q -r requirements.txt
echo "✅ Dependencies installed"

# Setup .env
if [ ! -f ".env" ]; then
    echo "📝 Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Edit .env and add your credentials:"
    echo "   • TELEGRAM_BOT_TOKEN"
    echo "   • TELEGRAM_CHAT_ID"
    echo "   • SUPABASE_URL"
    echo "   • SUPABASE_KEY"
    echo ""
fi

# Show next steps
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo "✅ Setup complete!"
echo ""
echo "📋 NEXT STEPS:"
echo ""
echo "1️⃣  Open .env and fill in your credentials:"
echo "   nano .env"
echo ""
echo "2️⃣  Create Supabase project (free at supabase.com):"
echo "   • Go to SQL Editor"
echo "   • Paste contents of: supabase_schema.sql"
echo "   • Run the query"
echo ""
echo "3️⃣  Create Telegram bot (message @BotFather on Telegram):"
echo "   • /newbot"
echo "   • Copy token to TELEGRAM_BOT_TOKEN"
echo "   • Add bot to channel and copy chat ID"
echo ""
echo "4️⃣  Test locally (optional):"
echo "   python main.py"
echo "   → Dashboard: http://localhost:5000"
echo ""
echo "5️⃣  Deploy to Render:"
echo "   • Push to GitHub"
echo "   • New Web Service on render.com"
echo "   • Connect your repo"
echo "   • Add environment variables from .env"
echo "   • Deploy!"
echo ""
echo "6️⃣  Setup UptimeRobot (keep Render awake):"
echo "   • Sign up at uptimerobot.com (free)"
echo "   • New HTTP Monitor: https://your-app.onrender.com/ping"
echo "   • Interval: 5 minutes"
echo ""
echo "═══════════════════════════════════════════════════════════════════════════"
echo ""
echo "Need help? See README.md"
