import { createClient } from '@/lib/supabase';
import { NextResponse } from 'next/server';
import { sendInitialGreeting, generateAIResponse } from '@/lib/ai-engine';

export async function POST(req: Request) {
  try {
    const body = await req.json();
    // Humne property_id aur propertyName dono ko destructure kiya hai
    const { name, phone, email, source, property_id, propertyName, interestedIn } = body;

    if (!name || !phone) {
      return NextResponse.json({ error: "Validation Failed: Name and Phone are required." }, { status: 400 });
    }

    const supabase = createClient();
    let finalPropertyId = property_id || null;

    // 2. Smart Property Lookup (Agar ID nahi hai toh Naam se dhoondo)
    if (!finalPropertyId && propertyName) {
      console.log(`🔍 Looking up property by name: ${propertyName}`);
      const { data: propertyData } = await supabase
        .from('properties')
        .select('id')
        .ilike('name', `%${propertyName}%`)
        .single();

      if (propertyData) {
        finalPropertyId = propertyData.id;
      }
    }

    // 3. Save Lead to Supabase
    console.log("--- Attempting to save lead ---");
    const { data: leadData, error: insertError } = await supabase
      .from('leads')
      .insert([{ 
        name, 
        phone, 
        email, 
        source: source || 'Direct API', 
        property_id: finalPropertyId, // Ye ab accurate ID bhejega
        status: 'New' 
      }])
      .select();

    if (insertError) {
      console.error("❌ SUPABASE INSERT ERROR:", insertError.message);
      throw insertError;
    }

    const newLead = leadData[0];
    console.log("✅ Lead saved successfully with ID:", newLead.id);

    // 4. THE MAGIC: SIMULATION TRIGGER (AWAIT added for accuracy)
    if (finalPropertyId) {
      console.log("--- Starting AI Processes (Greeting + AI Response) ---");
      
      // Greeting bhejo aur wait karo
      await sendInitialGreeting(name, finalPropertyId, interestedIn, newLead.id);
      console.log("📩 Greeting sent and saved to history.");

      // AI Response simulate karo aur wait karo
      // const testQuestion = "What are the key amenities and floor-wise pricing?";
      // console.log(`🤖 Simulating user question: "${testQuestion}"`);
      // await generateAIResponse(newLead.id, testQuestion, finalPropertyId);
      
      console.log("🏁 All background processes finished successfully.");
    } else {
      console.warn("⚠️ Warning: finalPropertyId is null. AI processes skipped.");
    }

    return NextResponse.json({ 
      success: true, 
      leadId: newLead.id,
      property_id: finalPropertyId,
      simulation: "AI Processes Completed"
    });

  } catch (error: any) {
    console.error("API Error:", error.message);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}