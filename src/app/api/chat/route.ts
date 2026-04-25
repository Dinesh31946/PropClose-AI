import { NextResponse } from 'next/server';
import { generateAIResponse } from '@/lib/ai-engine';

/**
 * This route allows us to simulate a conversation.
 * We send a message and propertyId, and the AI answers from the PDF.
 */
export async function POST(req: Request) {
  try {
    const { message, propertyId, leadId, interestedIn } = await req.json();

    if (!message || !propertyId) {
      return NextResponse.json({ error: "Message and Property ID are required" }, { status: 400 });
    }

    console.log(`\n[USER REPLY]: ${message}`);

    // Call the AI Brain to get the answer from the brochure
    const aiResponse = await generateAIResponse(leadId || "test-lead", message, propertyId, interestedIn);

    return NextResponse.json({ 
      success: true, 
      reply: aiResponse 
    });

  } catch (error: any) {
    console.error("Chat Simulation Error:", error.message);
    return NextResponse.json({ error: "Failed to generate response" }, { status: 500 });
  }
}