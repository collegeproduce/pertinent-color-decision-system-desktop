"""
CSV Exporter - Generate spreadsheet reports matching required format

Format (per screenshot):
Column A: File Names
Column B: Total Pages
Column C: Status (BW = all B&W, Color = all color, Partial = mixture)
Column D: Color page (comma-separated list of color page numbers)
Column E: Notes (aggregated reasons for color pages)
Column F: Total B/W
Column G: Total Color
"""

import csv
from pathlib import Path
from typing import List, Dict
from models import DocumentResult, PrintMode


class CSVExporter:
    """
    Generates CSV reports in the required format.
    """
    
    def __init__(self):
        self.documents_data: List[Dict] = []
    
    def add_document(
        self,
        filename: str,
        result: DocumentResult,
        doc_id: str = None
    ) -> None:
        """
        Add a document to the export queue.
        
        Args:
            filename: Original PDF filename
            result: DocumentResult with decisions
            doc_id: Optional document identifier
        """
        # Calculate aggregated data
        total_pages = result.total_pages
        color_pages_list = result.get_color_pages()
        bw_pages_count = len(result.get_bw_pages())
        color_pages_count = len(color_pages_list)
        
        # Determine status: BW (all B&W), Color (all color), Partial (mixture)
        if color_pages_count == 0:
            status = "BW"
        elif color_pages_count == total_pages:
            status = "Color"
        else:
            status = "Partial"
        
        # Format color page list (e.g., "1,2,3,4,5")
        color_page_str = ",".join(map(str, color_pages_list)) if color_pages_list else ""
        
        # Aggregate notes for color pages
        notes = self._generate_notes(result, color_pages_list)
        
        # Add to documents list
        self.documents_data.append({
            'File Names': filename,
            'Total Pages': total_pages,
            'Status': status,
            'Color page': color_page_str,
            'Notes': notes,
            'Total B/W': bw_pages_count,
            'Total Color': color_pages_count
        })
    
    def _generate_notes(
        self,
        result: DocumentResult,
        color_pages: List[int]
    ) -> str:
        """
        Generate aggregated notes for color pages.
        
        Args:
            result: DocumentResult
            color_pages: List of color page numbers
            
        Returns:
            Formatted notes string
        """
        if not color_pages:
            return ""
        
        notes_parts = []
        
        for page_id in color_pages:
            # Find the page record
            page_record = next(
                (p for p in result.pages if p.page_id == page_id),
                None
            )
            
            if page_record:
                # Format: "Page X: reason"
                reason = page_record.decision_details
                
                # Shorten very long reasons
                if len(reason) > 150:
                    reason = reason[:147] + "..."
                
                notes_parts.append(f"Page {page_id}: {reason}")
        
        # Join with newlines for better Excel display
        # (Excel will show this in multiple lines within the cell)
        return " | ".join(notes_parts)
    
    def export_to_csv(self, output_path: str) -> None:
        """
        Export all documents to CSV file.
        
        Args:
            output_path: Path to output CSV file
        """
        if not self.documents_data:
            raise ValueError("No documents to export")
        
        # Define column order
        fieldnames = [
            'File Names',
            'Total Pages',
            'Status',
            'Color page',
            'Notes',
            'Total B/W',
            'Total Color'
        ]
        
        # Write CSV
        with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            # Write header
            writer.writeheader()
            
            # Write data rows
            for doc_data in self.documents_data:
                writer.writerow(doc_data)
        
        print(f"\n✅ CSV exported to: {output_path}")
        print(f"   Total documents: {len(self.documents_data)}")
    
    def clear(self) -> None:
        """Clear all documents from export queue."""
        self.documents_data = []
    
    def get_summary(self) -> Dict:
        """
        Get summary statistics for all documents.
        
        Returns:
            Dictionary with summary stats
        """
        total_docs = len(self.documents_data)
        total_pages = sum(d['Total Pages'] for d in self.documents_data)
        total_color_pages = sum(d['Total Color'] for d in self.documents_data)
        total_bw_pages = sum(d['Total B/W'] for d in self.documents_data)
        
        all_bw_docs = sum(1 for d in self.documents_data if d['Status'] == 'BW')
        all_color_docs = sum(1 for d in self.documents_data if d['Status'] == 'Color')
        partial_docs = sum(1 for d in self.documents_data if d['Status'] == 'Partial')

        return {
            'total_documents': total_docs,
            'total_pages': total_pages,
            'total_color_pages': total_color_pages,
            'total_bw_pages': total_bw_pages,
            'documents_all_bw': all_bw_docs,
            'documents_all_color': all_color_docs,
            'documents_partial': partial_docs,
            'documents_with_color': all_color_docs + partial_docs,
        }


class ExcelExporter(CSVExporter):
    """
    Enhanced exporter with Excel formatting (future enhancement).
    
    For now, generates CSV that can be opened in Excel.
    Future: Use openpyxl for:
    - Conditional formatting (color-coded status)
    - Summary sheet
    - Formulas
    - Cell styling
    """
    
    def export_to_excel(self, output_path: str) -> None:
        """
        Export to Excel file with formatting.
        
        Note: Currently exports CSV. Future: Use openpyxl for true .xlsx
        """
        # For now, just export as CSV
        # User can open in Excel and save as .xlsx
        csv_path = output_path.replace('.xlsx', '.csv')
        self.export_to_csv(csv_path)
        
        print(f"\n💡 Tip: Open {Path(csv_path).name} in Excel and save as .xlsx for formatting")
