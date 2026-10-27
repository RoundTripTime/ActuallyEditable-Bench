\ProvidesClass{kcc}[2010/12/01 v1 thesis class for SNU CSE]

\newif\if@preprint

% load default class and set default paper size and font size
\DeclareOption{onecolumn}{\OptionNotUsed}
\DeclareOption{preprint}{\@preprinttrue}
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax

\LoadClass[10pt,a4paper,twocolumn]{article}

% set default paper margin
\usepackage[top=3cm, left=1cm, right=1cm, bottom=2cm]{geometry}

% XeLaTeX/Tectonic: fontspec + xeCJK (Nanum + Liberation Serif).
% pdfLaTeX: original ko.TeX/dhucs path from the KCC template.
\RequirePackage{iftex}
\ifXeTeX
  \usepackage{fontspec}
  \usepackage{xeCJK}
  \setmainfont{Liberation Serif}
  \setsansfont{Liberation Sans}
  \setCJKmainfont{NanumMyeongjoWeb}[
    BoldFont={NanumMyeongjo Bold},
    AutoFakeSlant=0.15
  ]
  \setCJKsansfont{NanumGothic}
  \xeCJKsetup{CJKspace=true}
  \renewcommand{\CJKecglue}{\hspace{0pt}}
  \usepackage{indentfirst}
  \usepackage{setspace}
  \setstretch{1.22}
  \usepackage{sectsty}
  \sectionfont{\fontsize{12}{12}\bfseries\selectfont}
  \subsectionfont{\fontsize{10}{10}\bfseries\selectfont}
  \subsubsectionfont{\fontsize{10}{10}\bfseries\selectfont}
\else
  \usepackage{times}
  \usepackage{dhucs-cmap}
  \usepackage[hangul,nonfrench,finemath]{kotex}
  \usepackage[hangul]{dhucs-setspace}
  \SetHangulspace{1.3}{1.1}
  \usepackage{indentfirst}
  \usepackage[parenthesis,hang]{dhucsfn}
  \usepackage[ensec]{dhucs-sectsty}
  \sectionfont{\fontsize{12}{12}\bfseries\selectfont}
  \subsectionfont{\fontsize{10}{10}\bfseries\selectfont}
  \subsubsectionfont{\fontsize{10}{10}\bfseries\selectfont}
\fi

\setlength{\parindent}{1em}
\setlength{\parskip}{0pt}

% do not generate page number
\pagestyle{empty}

%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% variable settings
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
\def\engtitle#1{\gdef\@engtitle{#1}}
\def\engauthor#1{\gdef\@engauthor{#1}}
\long\def\abstract#1{\gdef\@abstract{#1}}


%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% 제목
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
\renewcommand{\maketitle}{
	\twocolumn[
	\begin{center}
		{\fontsize{16}{16}\selectfont {\bfseries {\@title}} \par}
 		\vspace{2mm}
 		\if@preprint
	 		\vspace{40pt}
		\else
			\ifx\@author\@empty
			\else
				{\fontsize{10}{10}\selectfont {\bfseries {\@author}}\par}
				\vspace{4mm}% 국문 블록과 영문 제목 사이 한 줄 간격(원본 템플릿에는 없음)
			\fi
		\fi
		{\fontsize{14}{14}\selectfont {\bfseries {\@engtitle}} \par}
 		\vspace{2mm}
 		\if@preprint
	 		\vspace{26pt}
		\else
			\ifx\@engauthor\@empty
			\else
				{\fontsize{10}{10}\selectfont {\bfseries {\@engauthor}}\par}
			\fi
		\fi
 		\vspace{3mm}
		{\bfseries 요 \ \ 약 \par}
 		\vspace{1mm}
		\parbox{15.8cm}{\fontsize{9}{9}\selectfont {\quad\@abstract} \par}
	\end{center}
	\vspace{5mm}
	]
}

% format with dot '.' after the section number and before the section title
\makeatletter
\def\@seccntformat#1{
	\@ifundefined{#1@cntformat}%
	{\csname the#1\endcsname\quad}% default
	{\csname #1@cntformat\endcsname}% individual control
}
\def\section@cntformat{\thesection.\quad}
\makeatother
