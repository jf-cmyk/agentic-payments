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
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, "Making DeFi Bankable | blocksize.info", 0, 0, "L")
        self.cell(0, 10, f"Page {self.page_no()}", 0, 0, "R")

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
    pdf.chapter_title("API Documentation")
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
    pdf.chapter_title("Data Catalog")
    pdf.body_text("Access 3,900+ institutional-grade market instruments across all asset classes.")
    
    services = [
        ("Real-Time VWAP", "9,400+ Crypto pairs including BTC/USD, ETH/USD."),
        ("Bid/Ask Snapshots", "Institutional order-book depth for liquid crypto assets."),
        ("FX Rates", "120+ currency pairs including majors and exotics."),
        ("US Equities", "US and Chinese stock market snapshots."),
        ("Precious Metals", "Spot prices for Gold (XAU), Silver (XAG), etc."),
    ]
    
    for title, desc in services:
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(*BLUE)
        pdf.cell(0, 7, title, 0, 1)
        pdf.set_font("helvetica", "", 10)
        pdf.set_text_color(*DARK_GREY)
        pdf.multi_cell(0, 5, desc)
        pdf.ln(3)

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
        ("Crypto Tier 1", "$0.002", "BTC, ETH, SOL"),
        ("Crypto Tier 2", "$0.004", "LINK, AVAX, DOT"),
        ("FX & Metals", "$0.005", "EURUSD, XAUUSD"),
        ("Equities", "$0.008", "AAPL, TSLA, BABA"),
    ]
    
    pdf.set_text_color(*BLACK)
    pdf.set_font("helvetica", "", 10)
    for tier, price, examples in rows:
        pdf.cell(60, 8, tier, 1)
        pdf.cell(60, 8, price, 1, 0, 'C')
        pdf.cell(70, 8, examples, 1, 1)
        
    pdf.ln(10)
    pdf.section_title("Free Access")
    pdf.body_text("Service discovery, ticker search, and instrument lists are always FREE.")

    pdf.output("docs/pdf/Blocksize_Pricing_Guide.pdf")
    print("Generated: Blocksize_Pricing_Guide.pdf")

if __name__ == "__main__":
    generate_docs()
    generate_catalog()
    generate_flow()
    generate_pricing()
