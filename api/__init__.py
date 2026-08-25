"""FastAPI HTTP layer for the RCLL AI-Assisted Search chatbot.

This package is the only Streamlit-free web layer. It wraps the portable
``core`` package (unchanged answer engine) behind one POST endpoint that
fulfils the contract the frontend already documents, and adds the concerns a
public AWS endpoint needs: input validation, HMAC answer signing, rate
limiting, structured logging, and best-effort Google Sheets logging.
"""
