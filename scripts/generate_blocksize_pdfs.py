import os
from fpdf import FPDF

class BlocksizePDF(FPDF):
    def header(self):
        self.image("docs/assets/logo.png", 10, 8, 33) # Assumes logo exists
        self.set_font("helvetica", "B", 15)
        self.cell(80)
        self.cell(30, 10, "Blocksize Capital", 0, 0, "C")
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", 0, 0, "C")

    def chapter_title(self, label):
        self.set_font("helvetica", "B", 12)
        self.set_fill_color(200, 220, 255)
        self.cell(0, 6, label, 0, 1, "L", True)
        self.ln(4)

    def section_title(self, label):
        self.set_font("helvetica", "B", 10)
        self.cell(0, 10, label.upper(), 0, 1, "L")

    def body_text(self, text):
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, text)
        self.ln()

def generate_introduction(pdf):
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

def generate_architecture(pdf):
    pdf.add_page()
    pdf.chapter_title("System Architecture")
    pdf.body_text("The x402 gateway acts as a middleware between the blockchain settlement layer and the institutional data feed. Below is the high-level schematic of the Agentic Data Economy:")
    
    try:
        pdf.image("docs/assets/architecture_diagram.png", x=15, y=pdf.get_y() + 5, w=180)
    except:
        pdf.body_text("[Architecture Diagram Asset Missing]")

def generate_swimlane(pdf):
    pdf.add_page()
    pdf.chapter_title("Operational Swimlane")
    pdf.body_text("The following diagram illustrates the institutional data flow and automated settlement cycle via the Iron Dome security layer:")
    
    try:
        pdf.image("docs/assets/swimlane_diagram.png", x=15, y=pdf.get_y() + 5, w=180)
    except:
        pdf.body_text("[Swimlane Diagram Asset Missing]")

def generate_endpoints(pdf):
    pdf.add_page()
    pdf.chapter_title("Available Endpoints")
    endpoints = [
        ("GET /v1/vwap/{pair}", "Volume Weighted Average Price for Crypto/FX."),
        ("GET /v1/equity/{ticker}", "Institutional Equity Snapshot data."),
        ("GET /v1/metal/{ticker}", "Spot pricing for precious metals."),
        ("GET /v1/commodity/{ticker}", "Global commodity benchmarks.")
    ]
    for ep, desc in endpoints:
        pdf.section_title(ep)
        pdf.body_text(desc)

def generate_docs():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    generate_introduction(pdf)
    generate_architecture(pdf)
    generate_swimlane(pdf)
    generate_endpoints(pdf)
    pdf.output("docs/pdf/Blocksize_API_Documentation.pdf")
    print("Generated: Blocksize_API_Documentation.pdf")

def generate_catalog():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Institutional Data Catalog")
    pdf.body_text("Our catalog covers 9,400+ instruments across all major asset classes.")
    # Content truncated for brevity in this scratch script
    pdf.output("docs/pdf/Blocksize_Data_Catalog.pdf")
    print("Generated: Blocksize_Data_Catalog.pdf")

def generate_flow():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Autonomous User Flow")
    pdf.body_text("Detailing the machine-to-machine interaction model.")
    pdf.output("docs/pdf/Blocksize_User_Flow.pdf")
    print("Generated: Blocksize_User_Flow.pdf")

def generate_pricing():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Pricing Guide")
    pdf.section_title("Bulk Credit Drawdown Economy")
    pdf.body_text("For institutional scale, pre-purchase credits at a discount.")
    pdf.output("docs/pdf/Blocksize_Pricing_Guide.pdf")
    print("Generated: Blocksize_Pricing_Guide.pdf")

def generate_agent_manual():
    pdf = BlocksizePDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.chapter_title("Agent Integration Guide")
    pdf.body_text("Definitive instructions for autonomous agents.")
    pdf.output("docs/pdf/Blocksize_Agent_Manual.pdf")
    print("Generated: Blocksize_Agent_Manual.pdf")

if __name__ == "__main__":
    generate_docs()
    generate_catalog()
    generate_flow()
    generate_pricing()
    generate_agent_manual()
