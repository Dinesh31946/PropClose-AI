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
    // 1. FETCH LEAD CONTEXT (Memory Fetch)
    // Hum database se lead ki detail nikaal rahe hain taaki humein pata chale woh kiske liye aaya tha
    const { data: lead } = await supabase
      .from('leads')
      .select('name, property_id') // Yahan agar tumne table mein 'interested_in' column banaya hai toh use select karo
      .eq('id', leadId)
      .single();

    // Note: Agar tumne database mein 'interested_in' store nahi kiya hai, 
    // toh hum chat history ke pehle message se context nikaal sakte hain.
    const { data: inventory } = await supabase.from('unit_inventory').select('*').eq('project_id', propertyId);
    const { data: property } = await supabase.from('properties').select('name, description').eq('id', propertyId).single();
    const { data: history } = await supabase.from('chat_history').select('role, content').eq('lead_id', leadId).order('created_at', { ascending: false }).limit(10);

    const inventoryText = (inventory as Unit[])?.map((u: Unit) => 
      `- ${u.unit_name}: ${u.configuration}, Floor ${u.floor_no}, Size: ${u.carpet_area}, Price: ${u.price}, Status: ${u.status}`
    ).join('\n') || "No specific unit inventory found.";

    const chatContext = history?.reverse().map((msg: any) => ({ role: msg.role, content: msg.content })) || [];

    // 2. THE MASTER PROMPT (With Permanent Context)
    const systemPrompt = `You are an elite Real Estate Consultant for "${property?.name}".
    
    PRIMARY CONTEXT:
    - Customer Name: ${lead?.name}
    - Initial Interest: ${interestedIn} (CRITICAL: Always prioritize this specific unit/config).
    
    INVENTORY DATA:
    ${inventoryText}
    
    BROCHURE DATA:
    ${property?.description}
    
    GUIDELINES (HUMAN-CENTRIC):
    - PERSONALIZATION: ${lead?.name} ne "${interestedIn}" ke liye inquiry ki hai. Agar woh "iska price" ya "availability" puche, toh sirf usi specific unit ka jawab do jo unke interest se match karta hai. 
    - NO ROBOTIC LISTS: Don't use bullet points ( - ) or "Rs." symbols excessively. Talk like you are chatting on WhatsApp. Use natural Hinglish.
    - RELEVANCE: Do not list other 1BHK/2BHK options unless the user specifically says "show me more" or if their initial choice is sold out.
    - TONE: Professional yet friendly. Instead of "Registration typically depends...", say "Registration ka exact calculation main hamare manager se confirm karwa ke batata hoon."
    - FORMATTING: Avoid "\n\n" between every line. Keep it concise.
    - HANDOFF: Use [HANDOFF_REQUIRED] only for legal/RERA/complex queries, and never show this tag to the user.`;

    // 3. AI Call
    const completion = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "system", content: systemPrompt }, ...chatContext, { role: "user", content: userMessage }],
    });

    const aiReply = completion.choices[0].message.content || "";
    const cleanReply = aiReply.replace(/\[HANDOFF_REQUIRED\]/g, '').trim();

    console.log("------------------------------------------");
    console.log(`🤖 AI REPLY TO LEAD ${leadId}:`);
    console.log(cleanReply);
    console.log("------------------------------------------");

    // 4. Save to History
    await supabase.from('chat_history').insert([
        { lead_id: leadId, role: 'user', content: userMessage },
        { lead_id: leadId, role: 'assistant', content: cleanReply }
    ]);

    return cleanReply;
  } catch (error: any) {
    console.error("AI Engine Error:", error.message);
    return "I'm checking that for you. One moment.";
  }
}