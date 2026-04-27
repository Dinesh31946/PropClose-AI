import { NextResponse } from 'next/server';
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

export async function POST(req: Request) {
  try {
    const { headers } = await req.json();

    const systemPrompt = `You are a real estate data expert. 
    Map the "User Headers" to our "System Columns" or "metadata".

    CORE SYSTEM COLUMNS:
    - unit_name (Flat/Shop No)
    - floor_no
    - configuration (1BHK, 2BHK, etc.)
    - carpet_area
    - price
    - status

    USER HEADERS: ${headers.join(', ')}

    RULES:
    1. If you find a column like "Building", "Project", or "Property", map it to "project_name". 👈 STRICT RULE
    2. If a header matches a CORE column, map it (e.g., {"Unit": "unit_name"}).
    3. If a header is extra but useful (e.g., "Facing", "Balcony", "Parking", "PLC"), map it to "metadata" (e.g., {"Facing": "metadata"}).
    4. Return ONLY a valid JSON object.
    5. Format: {"ExcelHeader": "MappedColumn"}`;

    const response = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [{ role: "system", content: systemPrompt }],
      response_format: { type: "json_object" }
    });

    const mapping = JSON.parse(response.choices[0].message.content || '{}');
    return NextResponse.json({ success: true, mapping });
  } catch (error: any) {
    return NextResponse.json({ success: false, error: error.message });
  }
}