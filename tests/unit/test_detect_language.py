"""Language detection for the corpus records (2026-10-16: two verified pairs had language 'unknown')."""
from formbench.corpus.classify import detect_language


def test_devanagari_nepali_card_is_ne():
    # Chiba city evacuation card, Nepali edition (hinannsyaka-donepa-ru.docx)
    assert detect_language("उदाहरण शिविर कार्ड मिति साल महीना दिन कृपया प्रत्येक घरको लागि सूची बनाउनुहोस्") == "ne"


def test_hindi_is_hi():
    assert detect_language("कृपया प्रत्येक परिवार के लिए सूची बनाएं और तारीख भरें यह फॉर्म है") == "hi"


def test_number_grid_has_no_linguistic_content():
    assert detect_language("____________ x 1 2 3 4 5 6 7 8 9 10 11 12 2 4 6 8 10 12") == "zxx"


def test_existing_languages_unchanged():
    assert detect_language("가나다라마바사아자차카타파하 신청서 작성 방법") == "ko"
    assert detect_language("Application form for the meeting minutes") == "en"
    assert detect_language("申請書の記入例です。氏名を記入してください") == "ja"
    assert detect_language("") == "unknown"
