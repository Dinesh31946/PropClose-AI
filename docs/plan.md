📄 PropClose.ai: Production Readiness Document
🎯 Current Progress (Where we are)
•	Lead Ingestion: Successful API for saving leads.
•	Knowledge Base: AI reads directly from the property description (Brochure context).
•	Memory: chat_history table stores and retrieves the last 6 messages for context.
•	Multilingual: AI responds in Hindi, Marathi, and Hinglish.
•	Handoff System: AI triggers a needs_attention flag in the DB using the [HANDOFF_REQUIRED] tag.
________________________________________
🛠️ Phase 1: Refactoring (Optimization & Cleaning)
Jab hum real product launch karenge, toh in files mein ye changes karne honge:
1. src/lib/ai-engine.ts (The Brain)
•	Chunking (Advanced RAG): Abhi hum poora brochure bhej rahe hain (Slow & Costly). Humein isse Vector Embeddings mein todna hai taaki sirf relevant 2-3 chunks hi OpenAI ko jayen.
•	Model Switching: Testing mein gpt-4o use kiya hai. Production mein gpt-4o-mini use karenge (90% cost savings).
•	Token Management: Chat history ko 6 messages se summarize karke long-term memory banani hai.
2. src/app/api/leads/route.ts (The Entry)
•	Remove Simulations: Jo humne testQuestion aur dummy AI trigger dala hai testing ke liye, use puri tarah delete karna hai.
•	Validation: Phone number format aur email validation ko aur robust banana hai.
________________________________________
🔗 Phase 2: Meta (WhatsApp) Integration
Jab Meta ka access mil jayega, tab hamara architecture aise badlega:
•	Webhook Route: Ek naya /api/webhook banega jo Meta se incoming messages receive karega.
•	Media Support: Abhi hum sirf text handle kar rahe hain. Baad mein AI ko user ki bheji hui images/audio samajhne ki power deni hogi.
•	Official Templates: Greeting message ko Meta ke approved templates se replace karna hoga.
________________________________________
📢 Phase 3: The Notification System (Human Alert)
Abhi sirf DB mein flag change hota hai. Production mein:
•	Real-time Alert: Jab needs_attention true ho, toh Broker ko turant WhatsApp/Telegram/Email notification jaye.
•	Lead Dashboard: Ek simple UI jahan Broker "Attention Required" wali leads ko filter karke chat takeover kar sake.
________________________________________
⚠️ Critical Edge Cases to Fix (Misleading Prevention)
•	Price Accuracy: Brochure mein price "Starting" hoti hai. Humein AI ko aur strict banana hai ki woh exact floor-wise pricing par "Human Handoff" trigger kare.
•	Possession Dates: Brochure purana ho sakta hai. Possession dates hamesha properties table ke ek dedicated column se uthani chahiye, description se nahi.

