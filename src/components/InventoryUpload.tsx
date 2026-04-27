'use client';
import React, { useState } from 'react';
import ExcelJS from 'exceljs';
import Papa from 'papaparse'; // CSV ke liye
import { Upload, FileDown, AlertCircle } from 'lucide-react';

interface InventoryUploadProps {
  onDataExtracted: (headers: string[], data: any[]) => void;
}

export default function InventoryUpload({ onDataExtracted }: InventoryUploadProps) {
  const [fileName, setFileName] = useState("");
  const [isDragging, setIsDragging] = useState(false);

  const handleFileUpload = async (e: any) => {
    const file = e.target.files?.[0] || e.dataTransfer?.files?.[0];
    if (!file) return;
    setFileName(file.name);

    const fileExtension = file.name.split('.').pop()?.toLowerCase();

    if (fileExtension === 'csv') {
      // --- CSV Logic ---
      Papa.parse(file, {
        header: true,
        skipEmptyLines: true,
        complete: (results) => {
          if (results.data.length > 0) {
            const headers = Object.keys(results.data[0] as object);
            onDataExtracted(headers, results.data);
          }
        },
      });
    } else {
      // --- Excel (.xlsx, .xls) Logic ---
      const workbook = new ExcelJS.Workbook();
      const reader = new FileReader();

      reader.onload = async (evt) => {
        const buffer = evt.target?.result as ArrayBuffer;
        await workbook.xlsx.load(buffer);
        const worksheet = workbook.worksheets[0];
        
        const rows: any[] = [];
        let headers: string[] = [];

        worksheet.eachRow((row, rowNumber) => {
            const rowValues = row.values as any[];
            if (rowNumber === 1) {
                // Index 1 se filter karna shuru karo
                headers = rowValues.filter((val, idx) => idx > 0 && val !== undefined);
            } else {
                const rowData: any = {};
                headers.forEach((header, index) => {
                // Index + 1 kyunki values 1-based hain
                const val = rowValues[index + 1];
                if (header) rowData[header] = val;
                });
                rows.push(rowData);
            }
        });
        if (headers.length > 0) onDataExtracted(headers, rows);
      };
      reader.readAsArrayBuffer(file);
    }
  };

  return (
    <div className="bg-background border border-border rounded-2xl shadow-sm overflow-hidden transition-all hover:shadow-md">
      <div className="p-4 md:p-6 border-b border-border flex justify-between items-center bg-accent/[0.02]">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 bg-accent rounded-full animate-pulse"></div>
          <h3 className="font-semibold text-sm md:text-base text-foreground">Import Inventory</h3>
        </div>
        <a 
          href="/templates/inventory_template.csv" 
          download 
          className="flex items-center gap-2 text-[10px] md:text-xs font-semibold text-accent hover:opacity-80 transition-opacity bg-accent/10 px-3 py-1.5 rounded-full"
        >
          <FileDown size={14} /> Download Template
        </a>
      </div>

      <div 
        className={`p-6 md:p-10 m-4 md:m-6 border-2 border-dashed rounded-xl transition-all flex flex-col items-center justify-center gap-4 group
          ${isDragging ? 'border-accent bg-accent/5' : 'border-border hover:border-accent/50'}
        `}
        onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(e) => { e.preventDefault(); setIsDragging(false); handleFileUpload(e); }}
      >
        <div className="p-3 md:p-4 bg-accent/5 rounded-2xl group-hover:scale-110 transition-transform duration-300">
          <Upload className="text-accent" size={28} />
        </div>
        <div className="text-center">
          <p className="text-sm md:text-lg font-medium text-foreground truncate max-w-[200px] md:max-w-md">
            {fileName ? fileName : "Drop your Excel or CSV here"}
          </p>
          <p className="text-[10px] md:text-sm text-muted-foreground mt-1">
            Fast processing for XLSX, XLS, and CSV
          </p>
        </div>
        <label className="mt-2 px-5 py-2 bg-foreground text-background rounded-full text-xs md:text-sm font-semibold cursor-pointer hover:opacity-90 transition-opacity">
          Select File
          <input type="file" accept=".xlsx, .xls, .csv" onChange={handleFileUpload} className="hidden" />
        </label>
      </div>

      <div className="px-6 py-3 bg-accent/2 border-t border-border flex items-center gap-2">
        <AlertCircle size={12} className="text-muted-foreground" />
        <p className="text-[10px] text-muted-foreground">
          AI will auto-detect headers like Unit Name, Price, and Configuration.
        </p>
      </div>
    </div>
  );
}