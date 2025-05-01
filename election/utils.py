# import pandas as pd
# from .models import ClassGroup, Voter
# from docx import Document
# import PyPDF2

# def extract_voters_from_excel(file):
#     df = pd.read_excel(file)
#     return df.to_dict('records')

# def extract_voters_from_word(file):
#     doc = Document(file)
#     data = []
#     for para in doc.paragraphs:
#         line = para.text.strip()
#         if line:
#             parts = line.split(',')
#             if len(parts) >= 2:
#                 data.append({'matric_number': parts[0].strip(), 'class_name': parts[1].strip()})
#     return data

# def extract_voters_from_pdf(file):
#     pdf = PyPDF2.PdfReader(file)
#     text = ""
#     for page in pdf.pages:
#         text += page.extract_text()
#     lines = text.split('\n')
#     data = []
#     for line in lines:
#         parts = line.split(',')
#         if len(parts) >= 2:
#             data.append({'matric_number': parts[0].strip(), 'class_name': parts[1].strip()})
#     return data

# def save_voter_list(data):
#     for entry in data:
#         class_name = entry.get('class_name')
#         matric_number = entry.get('matric_number')
#         if class_name and matric_number:
#             class_group, _ = ClassGroup.objects.get_or_create(name=class_name)
#             Voter.objects.get_or_create(matric_number=matric_number, class_group=class_group)

import pandas as pd
from docx import Document
import PyPDF2
import re
from .models import ClassGroup, Voter

def extract_voters_from_excel(file_path):
    df = pd.read_excel(file_path)
    # Expecting columns: matric_number, class_name
    return df.to_dict('records')

def extract_voters_from_word(file_path):
    doc = Document(file_path)
    data = []

    print("DEBUG: Scanning Word tables...")

    for table in doc.tables:
        for row in table.rows:
            cells = row.cells
            if len(cells) < 2:
                continue

            for cell in cells:
                line = cell.text.strip()
                match = re.search(r'(PHA/\d{4}/\d{2})', line, re.IGNORECASE)
                if match:
                    matric_number = match.group(1).upper()
                    data.append({
                        'matric_number': matric_number,
                        'class_name': 'Level 600'
                    })
                    # print(f"✅ Found: {matric_number}")
                    break  # move to next row after finding a matric number

    # print(f"FINAL DATA: {data}")
    return data



def extract_voters_from_pdf(file_path):
    pdf = PyPDF2.PdfReader(file_path)
    text = ""
    for page in pdf.pages:
        text += page.extract_text()
    lines = text.split('\n')
    data = []
    for line in lines:
        parts = line.split(',')
        if len(parts) >= 2:
            data.append({'matric_number': parts[0].strip(), 'class_name': 'Level 600'})
    return data

def save_voter_list(data):
    # Ensure class group exists once
    class_group, _ = ClassGroup.objects.get_or_create(name='Level 600')

    for entry in data:
        matric_number = entry.get('matric_number')
        if matric_number:
            voter, created = Voter.objects.get_or_create(
                matric_number=matric_number,
                defaults={'class_group': class_group}
            )
            print(f"{'Created' if created else 'Skipped'}: {matric_number}")
