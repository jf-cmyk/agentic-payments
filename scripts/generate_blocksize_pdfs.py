import os
from fpdf import FPDF # type: ignore

# BRAND COLORS (blocksize.info)
BLUE = (79, 75, 255)       # #4f4bff
YELLOW = (236, 247, 129)   # #ecf781
GREY = (246, 246, 246)     # #f6f6f6
BLACK = (0, 0, 0)          # #000000
DARK_GREY = (40, 40, 40)

class BlocksizePDF(FPDF):
    def header(self):
        # Clean white header with accent line and logo
        self.set_draw_color(*BLUE)
        self.set_line_width(0.5)
        self.line(10, 15, 200, 15)
        
        # Logo Image
        logo_path = "docs/assets/logo.png"
        if os.path.exists(logo_path):
            self.image(logo_path, x=10, y=7, h=5)
        
        # Reference text (shifted right of logo)
        self.set_font("helvetica", "B", 8)
        self.set_text_color(*BLACK)
        self.set_xy(50, 8)
        self.cell(0, 5, "x402 DATA ECONOMY | blocksize.info", 0, 0, "R")
        
        self.ln(15)

    def footer(self):
        self.set_y(-20)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, "Blocksize Capital GmbH | Taunusanlage 8, 60329 Frankfurt | Directors: C. Labetzsch, C. Impekoven", 0, 1, "C")
        self.cell(0, 5, "HRB 113470 | VAT DE321106382 | agentic-data.blocksize.info", 0, 0, "C")

    def chapter_title(self, label):
        self.set_font("helvetica", "B", 16)
        self.set_text_color(*BLUE)
        self.cell(0, 10, label.upper(), 0, 1, "L")
        self.ln(4)

    def section_title(self, label):
        self.set_font("helvetica", "B", 12)
        self.set_text_color(*BLACK)
        self.cell(0, 8, label, 0, 1, "L")
        self.ln(2)

    def body_text(self, text):
        self.set_font("helvetica", "", 10)
        self.set_text_color(*DARK_GREY)
        self.multi_cell(0, 6, text)
        self.ln(4)

def generate_docs():
    pdf = BlocksizePDF()
    pdf.add_page()
    pdf.chapter_title("Agentic Market Data Documentation")
    pdf.section_title("What is x402?")
    pdf.body_text(
        "The x402 protocol is a pay-per-call institutional data gateway. It allows autonomous "
        "AI agents to pay for financial data in real-time using USDC stablecoins. "
        "There are no monthly subscriptions; you only pay for the exact data points consumed."
    )
    
    pdf.section_title("The 402 Protocol Cycle")
    pdf.body_text(
        "1. Request: Agent requests a data endpoint without an API key.\n"
        "2. Intercept: Server returns HTTP 402 Payment Required.\n"
        "3. Metadata: Server includes PAYMENT-REQUIRED header with amount and destination.\n"
        "4. Settlement: Agent sends USDC via Solana or Base L2.\n"
        "5. Proof: Agent resubmits with PAYMENT-SIGNATURE header containing the TX hash.\n"
        "6. Data: Server verifies and returns the institutional payload."
    )
    
    pdf.section_title("Supported Networks")
    pdf.body_text(
        "- Solana Mainnet (Fastest settlement, low fees)\n"
        "- Base L2 (EVM-native fallback)"
    )
    
    os.makedirs("docs/pdf", exist_ok=True)
    pdf.output("docs/pdf/Blocksize_API_Documentation.pdf")
    print("Generated: Blocksize_API_Documentation.pdf")

def generate_catalog():
    pdf = BlocksizePDF()
    pdf.add_page()
    pdf.chapter_title("Digital Asset Catalog")
    pdf.body_text("Access 3,900+ institutional-grade market instruments across all asset classes.")
    
    # Detailed Asset Groups
    groups = [
        ("Core Crypto (Tier 1)", "BTCUSD, ETHUSD, SOLUSD, XRPUSD, ADAUSD, DOGEUSD, DOTUSD, AVAXUSD, SHIBUSD, LINKUSD"),
        ("Extended & State (Tier 2)", "JUPUSD, WIFUSD, GNSUSD, TURBOUSD, PYTHUSD, BONKUSD, RNDRUSD, TIAUSD, SEIUSD, HNTUSD"),
        ("FX Majors & Exotics", "EURUSD, GBPUSD, JPYUSD, AUDUSD, CHFUSD, USDCAD, EURGBP, EURJPY, USDMXN, USDZAR"),
        ("Precious Metals", "XAUUSD (Gold), XAGUSD (Silver), XPTUSD (Platinum), XPDUSD (Palladium), COPPERUSD"),
        ("Global Commodities", "BRENT (Oil), WHEAT, CORN, SUGAR, COFFEE, NATGAS"),
        ("US Equities", "AAPL, TSLA, NVDA, MSFT, AMZN, GOOGL, META, NFLX, AMD, BABA")
    ]
    
    for title, tickers in groups:
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(*BLUE)
        pdf.cell(0, 7, title, 0, 1)
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(*DARK_GREY)
        # Use a boxed display for tickers to look like a catalog
        pdf.multi_cell(0, 5, f"Supported Tickers: {tickers}")
        pdf.ln(4)

    pdf.section_title("Real-Time Capabilities")
    pdf.body_text(
        "Every instrument listed is available via real-time VWAP and order-book snapshot endpoints. "
        "Agents can discover the full 3,900+ instrument list programmatically via the /v1/vwap/instruments endpoint."
    )

    pdf.output("docs/pdf/Blocksize_Data_Catalog.pdf")
    print("Generated: Blocksize_Data_Catalog.pdf")

def generate_flow():
    pdf = BlocksizePDF()
    pdf.add_page()
    pdf.chapter_title("Autonomous User Flow")
    pdf.body_text("Optimized for LLM-led agents and automated trading systems.")
    
    steps = [
        ("Step 1: Resource Access", "Agent requests /v1/vwap/BTCUSD."),
        ("Step 2: Payment Challenge", "Server returns 402 with 'PAYMENT-REQUIRED' header metadata."),
        ("Step 3: Settlement", "Agent signs and broadcasts USDC transfer to specified wallet."),
        ("Step 4: Data Unlock", "Agent resubmits request with cryptographic proof of payment.")
    ]
    
    for title, desc in steps:
        pdf.set_draw_color(*BLUE)
        pdf.set_line_width(0.5)
        pdf.rect(pdf.get_x(), pdf.get_y(), 190, 20)
        pdf.set_xy(pdf.get_x() + 5, pdf.get_y() + 5)
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(0, 5, title, 0, 1)
        pdf.set_font("helvetica", "", 9)
        pdf.set_xy(pdf.get_x() + 5, pdf.get_y())
        pdf.cell(0, 5, desc, 0, 1)
        pdf.ln(10)

    pdf.output("docs/pdf/Blocksize_User_Flow.pdf")
    print("Generated: Blocksize_User_Flow.pdf")

def generate_pricing():
    pdf = BlocksizePDF()
    pdf.add_page()
    pdf.chapter_title("Pricing Guide")
    pdf.body_text("Clear, transparent, pay-per-call pricing model.")
    
    # Header row
    pdf.set_fill_color(*BLUE)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(60, 10, "Service Tier", 1, 0, 'C', True)
    pdf.cell(60, 10, "Price (USDC)", 1, 0, 'C', True)
    pdf.cell(70, 10, "Example Assets", 1, 1, 'C', True)
    
    # Data rows
    rows = [
        ("Core Crypto", "$0.002", "BTCUSD, ETHUSD, SOLUSD"),
        ("Extended & State", "$0.004", "JUPUSD, WIFUSD, GNSUSD"),
        ("FX & Metals", "$0.005", "EURUSD, XAUUSD"),
        ("Commodities", "$0.005", "BRENT, WHEAT, CORN"),
        ("Equities", "$0.008", "AAPL, TSLA, BABA"),
    ]
    
    pdf.set_text_color(*BLACK)
    pdf.set_font("helvetica", "", 10)
    for tier, price, examples in rows:
        pdf.cell(60, 8, tier, 1)
        pdf.cell(60, 8, price, 1, 0, 'C')
        pdf.cell(70, 8, examples, 1, 1)
        
    pdf.ln(10)
    pdf.section_title("GTM: Licensed Agent Trial")
    pdf.body_text("To prevent Sybil-attacks and ensure data integrity, the 50-credit Free Trial is reserved for verified agent wallets with an on-chain stake of >0.1 SOL. This trial is strictly limited to one provision per unique IP address.")
    
    pdf.ln(5)
    pdf.section_title("Bulk Credit Drawdown Economy")
    pdf.body_text("For institutional scale and automated agent workflows, pre-purchase credits at a discount to eliminate per-call settlement latency.")
    
    # Credit tiers
    pdf.set_fill_color(*GREY)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(60, 8, "Tier Package", 1, 0, 'L', True)
    pdf.cell(60, 8, "Credits", 1, 0, 'C', True)
    pdf.cell(70, 8, "Purchase Price", 1, 1, 'C', True)
    
    pdf.set_font("helvetica", "", 9)
    credit_tiers = [
        ("Starter Pouch", "1,000", "$0.90 USDC (10% OFF)"),
        ("Growth Pack", "10,000", "$8.00 USDC (20% OFF)"),
        ("Institutional", "100,000", "$60.00 USDC (40% OFF)")
    ]
    for name, qty, price in credit_tiers:
        pdf.cell(60, 7, name, 1)
        pdf.cell(60, 7, qty, 1, 0, 'C')
        pdf.cell(70, 7, price, 1, 1, 'C')

    pdf.ln(10)
    pdf.section_title("Free Access")
    pdf.body_text("Service discovery, ticker search, and instrument lists are always FREE.")

    pdf.output("docs/pdf/Blocksize_Pricing_Guide.pdf")
    print("Generated: Blocksize_Pricing_Guide.pdf")

def generate_architecture(pdf):
    pdf.add_page()
    pdf.chapter_title("System Architecture")
    pdf.body_text("The x402 gateway acts as a middleware between the blockchain settlement layer and the institutional data feed. Below is the high-level schematic of the Agentic Data Economy:")
    
    # Embed the high-fidelity diagram
    try:
        pdf.image("docs/assets/architecture_diagram.png", x=15, y=pdf.get_y() + 5, w=180)
    except:
        pdf.body_text("[Architecture Diagram Asset Missing]")

def generate_agent_manual():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Agent Integration Guide")
    pdf.body_text("This guide provides autonomous agents with the instructions necessary to discover, purchase, and consume institutional-grade market data via the x402 payment protocol.")
    
    pdf.ln(5)
    pdf.section_title("1. Automated Discovery")
    pdf.body_text("Agents can discover all available tools and pricing by fetching the machine-readable manifest at /mcp/manifest.json.")
    
    pdf.ln(5)
    pdf.section_title("2. Performance Settlement (Credits)")
    pdf.body_text("For high-frequency workflows, use the Credits system. Provision a wallet with 0.1 SOL and 24h history to receive the 50-credit Welcome Pack automatically.")
    
    pdf.ln(5)
    pdf.section_title("3. Institutional Security (Iron Dome)")
    pdf.body_text("Access requires a verified on-chain stake of >0.1 SOL and a minimum wallet age of 24 hours. Permanent IP guardrails are enforced to maintain data integrity.")
    
    pdf.set_y(-60)
    pdf.section_title("Technical Manual Ready")
    pdf.body_text("For full API specs, refer to the Blocksize API Documentation PDF or the Swagger landing page.")
    
    pdf.output("docs/pdf/Blocksize_Agent_Manual.pdf")
    print("Generated: Blocksize_Agent_Manual.pdf")

if __name__ == "__main__":
    generate_docs()
    generate_catalog()
    generate_flow()
    generate_pricing()
    generate_agent_manual()
