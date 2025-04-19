import pandas as pd
from .models import ClassGroup, Voter
from docx import Document
import PyPDF2

def extract_voters_from_excel(file):
    df = pd.read_excel(file)
    return df.to_dict('records')

def extract_voters_from_word(file):
    doc = Document(file)
    data = []
    for para in doc.paragraphs:
        line = para.text.strip()
        if line:
            parts = line.split(',')
            if len(parts) >= 2:
                data.append({'matric_number': parts[0].strip(), 'class_name': parts[1].strip()})
    return data

def extract_voters_from_pdf(file):
    pdf = PyPDF2.PdfReader(file)
    text = ""
    for page in pdf.pages:
        text += page.extract_text()
    lines = text.split('\n')
    data = []
    for line in lines:
        parts = line.split(',')
        if len(parts) >= 2:
            data.append({'matric_number': parts[0].strip(), 'class_name': parts[1].strip()})
    return data

def save_voter_list(data):
    for entry in data:
        class_name = entry.get('class_name')
        matric_number = entry.get('matric_number')
        if class_name and matric_number:
            class_group, _ = ClassGroup.objects.get_or_create(name=class_name)
            Voter.objects.get_or_create(matric_number=matric_number, class_group=class_group)

