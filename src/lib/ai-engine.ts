import { createClient } from '@/lib/supabase';
import OpenAI from 'openai';

// TypeScript Types: Taki code mein red lines na aayein
interface Unit {
  unit_name: string;
  configuration: string;
  floor_no: number;
  carpet_area: string;
  price: string;
  status: string;
}

interface ChatMessage {
  role: string;
  content: string;
}

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

export async function sendInitialGreeting(leadName: string, propertyId: string, interestedIn: string, leadId: string) {
  const supabase = createClient();
  
  try {
    const { data: property } = await supabase.from('properties').select('name').eq('id', propertyId).single();

    // Greeting ko personalized banaya
    const welcomeMsg = `Hi ${leadName}! 😊 Thank you for your interest in ${property?.name}. I see you are looking for ${interestedIn}. I am your dedicated assistant. How can I help you with the pricing or floor plans today?`;
    
    await supabase.from('chat_history').insert([
      { lead_id: leadId, role: 'assistant', content: welcomeMsg }
    ]);

    console.log("------------------------------------------");
    console.log(`🤖 AI REPLY TO LEAD ${leadId}:`);
    console.log(welcomeMsg);
    console.log("------------------------------------------");

    return welcomeMsg;
  } catch (error) {
    console.error("Greeting Error:", error);
    return "Hi! How can I help you with this project?";
  }
}

export async function generateAIResponse(leadId: string, userMessage: string, propertyId: string, interestedIn: string) {
  const supabase = createClient();

  try {
    // 1. FETCH BASIC CONTEXT (Lead & History)
    const { data: lead } = await supabase.from('leads').select('name').eq('id', leadId).single();
    const { data: property } = await supabase.from('properties').select('name').eq('id', propertyId).single();
    const { data: history } = await supabase.from('chat_history')
      .select('role, content')
      .eq('lead_id', leadId)
      .order('created_at', { ascending: false })
      .limit(6);

    const chatContext = history?.reverse().map((msg: any) => ({ role: msg.role, content: msg.content })) || [];

    // 2. SEARCH ENGINE CALL (Internal Vector Search)
    // Hum userMessage ko vector mein badal kar relevant data layenge
    const embeddingResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: userMessage,
    });
    const queryEmbedding = embeddingResponse.data[0].embedding;

    // Parallel Database Search
    const [unitResult, chunkResult] = await Promise.all([
      supabase.rpc('match_units', { query_embedding: queryEmbedding, match_threshold: 0.3, match_count: 5 }),
      supabase.rpc('match_chunks', { query_embedding: queryEmbedding, match_threshold: 0.3, match_count: 3 })
    ]);

    // Format for GPT (Lean & Mean)
    const contextUnits = unitResult.data?.map((u: any) => 
      `- Unit ${u.unit_name}: ${u.ai_summary} (Confidence: ${Math.round(u.similarity * 100)}%)`
    ).join('\n') || "No matching units found.";

    const contextKnowledge = chunkResult.data?.map((c: any) => 
      `- Info: ${c.content.replace(/[^\x20-\x7E]/g, '').substring(0, 500)}`
    ).join('\n') || "No specific brochure details found.";

    // 3. MASTER SYSTEM PROMPT (The Personality)
    const systemPrompt = `You are an elite Real Estate Consultant for "${property?.name}".
    
    CONTEXT DATA:
    - Customer: ${lead?.name}
    - Initial Interest: ${interestedIn}
    
    RELEVANT INVENTORY (Live Data):
    ${contextUnits}
    
    PROJECT KNOWLEDGE (Brochure):
    ${contextKnowledge}
    
    STRICT RULES:
    1. ACCURACY: Sirf upar diye gaye data se jawab do. Agar data "Confidence" 40% se kam hai, toh kaho ki aap check karke batayenge.
    2. HINGLISH: Talk like a professional Mumbai broker on WhatsApp. Use natural Hinglish.
    3. NO LISTS: Don't give long tables. Be concise.
    4. RENT/SALE: Data mein check karo ki listing Rent ki hai ya Sale ki aur usi hisab se baat karo.
    5. HALLUCINATION: Agar info nahi hai, toh jhoot mat bolo. [HANDOFF_REQUIRED] use karo agar query complex hai.`;

    // 4. AI Call
    const completion = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [
        { role: "system", content: systemPrompt },
        ...chatContext,
        { role: "user", content: userMessage }
      ],
      temperature: 0.4, // Lower temperature for higher accuracy
    });

    const aiReply = completion.choices[0].message.content || "";
    const cleanReply = aiReply.replace(/\[HANDOFF_REQUIRED\]/g, '').trim();

    // 5. SAVE HISTORY
    await supabase.from('chat_history').insert([
        { lead_id: leadId, role: 'user', content: userMessage },
        { lead_id: leadId, role: 'assistant', content: cleanReply }
    ]);

    return cleanReply;

  } catch (error: any) {
    console.error("AI Engine Error:", error.message);
    return "Thoda technical issue hai, main ek minute mein batata hoon.";
  }
}