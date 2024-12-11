import re
from pathlib import Path

import tiktoken
from bs4 import BeautifulSoup, NavigableString, Tag

# 不需要翻译的标签集合
SKIP_TAGS = {
    # 脚本和样式
    "script",
    "style",
    # 代码相关
    "code",
    # "pre",
    "kbd",
    "var",
    "samp",
    # 特殊内容
    "svg",
    "math",
    "canvas",
    "address",
    "applet",
    # 多媒体标签
    "img",
    "audio",
    "video",
    "track",
    "source",
    # 表单相关
    "input",
    "button",
    "select",
    "option",
    "textarea",
    "form",
    # 元数据和链接
    "meta",
    "link",
    # "a",
    # 嵌入内容
    "iframe",
    "embed",
    "object",
    "param",
    # 技术标记
    "time",
    "data",
    "meter",
    "progress",
    # XML相关
    "xml",
    "xmlns",
    # EPUB特有标签
    "epub:switch",
    "epub:case",
    "epub:default",
    # 注释标签
    "annotation",
    "note",
}


class HTMLAnalyzer:
    def __init__(self):
        self.placeholder_counter = 0
        self.placeholders = {}
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def create_placeholder(self, content: str) -> str:
        placeholder = f"†{self.placeholder_counter}†"
        self.placeholders[placeholder] = content
        self.placeholder_counter += 1
        return placeholder

    def replace_skip_tags_recursive(self, node: Tag) -> None:
        if not node or isinstance(node, NavigableString):
            return

        for child in list(node.children):
            if isinstance(child, Tag):
                if child.name in SKIP_TAGS:
                    placeholder = self.create_placeholder(str(child))
                    child.replace_with(placeholder)
                else:
                    self.replace_skip_tags_recursive(child)

    def analyze_node(self, node, path="", depth=0, min_tokens=200):
        if not node:
            return (0, 0)  # Return (total_tokens, text_tokens)

        if isinstance(node, Tag) and node.name in SKIP_TAGS:
            return (0, 0)

        if isinstance(node, NavigableString):
            return (0, 0)

        indent = "  " * depth

        # Get text content
        content_text = "".join(node.stripped_strings)
        content_without_placeholders = re.sub(r"†\d+†", "", content_text)

        if not content_without_placeholders.strip():
            return (0, 0)

        # Calculate token metrics
        content = str(node)
        total_tokens = len(self.tokenizer.encode(content))
        text_tokens = len(self.tokenizer.encode(content_text))

        # Get direct text metrics
        direct_text = "".join(
            str(child)
            for child in node.children
            if isinstance(child, NavigableString) and str(child).strip()
        )
        direct_text_without_placeholders = re.sub(r"†\d+†", "", direct_text)
        direct_text_tokens = (
            len(self.tokenizer.encode(direct_text))
            if direct_text_without_placeholders.strip()
            else 0
        )

        # Calculate child metrics
        child_total_tokens = 0
        child_text_tokens = 0
        mergeable_children = []

        for child in node.children:
            if isinstance(child, Tag):
                child_tokens, child_text = self.analyze_node(
                    child,
                    f"{path}/{node.name}" if path else node.name,
                    depth + 1,
                    min_tokens,
                )
                child_total_tokens += child_tokens
                child_text_tokens += child_text
                if 0 < child_tokens <= 6000:
                    mergeable_children.append((child, child_tokens))

        # Display analysis if meets threshold
        if total_tokens >= min_tokens:
            print(f"\n{indent}Level {depth}: <{node.name}>")
            print(
                f"{indent}Path: {path}/{node.name}"
                if path
                else f"{indent}Path: {node.name}"
            )
            print(f"{indent}Total tokens: {total_tokens}")
            print(f"{indent}Text tokens: {text_tokens}")
            print(f"{indent}Direct text tokens: {direct_text_tokens}")
            print(f"{indent}Child nodes total tokens: {child_total_tokens}")
            print(f"{indent}Child nodes text tokens: {child_text_tokens}")
            print(f"{indent}Text/Total ratio: {text_tokens/total_tokens:.2%}")

            # Translation strategy recommendation
            if total_tokens <= 6000:
                if direct_text_tokens > 0:
                    print(f"{indent}Strategy: Translate node as whole unit")
                    if len(direct_text) > 100:
                        print(f"{indent}Direct text preview: {direct_text[:100]}...")
                    else:
                        print(f"{indent}Direct text: {direct_text}")
                elif mergeable_children:
                    print(f"{indent}Strategy: Consider merging these child nodes:")
                    cumulative_tokens = 0
                    merge_group = []
                    for child, tokens in mergeable_children:
                        if cumulative_tokens + tokens <= 6000:
                            merge_group.append(child.name)
                            cumulative_tokens += tokens
                        else:
                            if merge_group:
                                print(
                                    f"{indent}  Group ({cumulative_tokens} tokens): {', '.join(merge_group)}"
                                )
                            merge_group = [child.name]
                            cumulative_tokens = tokens
                    if merge_group:
                        print(
                            f"{indent}  Group ({cumulative_tokens} tokens): {', '.join(merge_group)}"
                        )
            else:
                print(f"{indent}Strategy: Process child nodes individually")

            # Content preview
            if len(content_text) > 100:
                print(f"{indent}Content preview: {content_text[:100]}...")
            else:
                print(f"{indent}Content: {content_text}")
            print()

        return (total_tokens, text_tokens)

    def analyze_html(self, html_content: str, min_tokens=200):
        # Reset state
        self.placeholder_counter = 0
        self.placeholders = {}

        # Parse HTML
        soup = BeautifulSoup(html_content, "html.parser")
        root = soup.find()

        if not root:
            return

        print("Analyzing HTML content")
        print("=" * 80)
        print("\nStep 1: Replace skip tags with placeholders")

        # Step 1: Replace all skip tags with placeholders
        self.replace_skip_tags_recursive(root)

        # Step 2: Analyze nodes
        print(f"\nStep 2: Analyzing nodes with token count >= {min_tokens}:")
        print(
            "(Note: Nodes marked with 'Will translate as whole node' will be translated entirely)"
        )
        self.analyze_node(root, min_tokens=min_tokens)


def main():
    # 测试用的 HTML 内容
    test_html = """
    <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en-US" xml:lang="en-US">
	<head>
		<title>B21583_31</title>
		<link href="css/style-JRserifv6.css" rel="stylesheet" type="text/css"/>
<meta content="urn:uuid:936af032-cf12-4bed-b156-1fda74f97f18" name="Adept.expected.resource"/>
	</head>
	<body id="B21583_31">
		<div id="_idContainer122">
			<p class="figure-caption"><img alt="" role="presentation" src="image/B21583_Case-Study-Graphic.png"/></p>
		</div>
		<div id="_idContainer124">
			<h1 class="chapter-number" id="_idParaDest-100" xml:lang="en-GB"><a id="_idTextAnchor173"/>31 <span class="P---Color">Case Study</span></h1>
			<h1 id="_idParaDest-101" xml:lang="en-GB"><a id="_idTextAnchor174"/>Minimizing Customer Churn with AI</h1>
			<p class="intro"><strong class="bold-intro"><a id="_idTextAnchor175"/>In conversation with Manu Kumar:</strong> Chief Data<a id="_idIndexMarker659"/> and AI Officer, with over a decade of experience in the advanced analytics and AI space. His expertise spans senior leadership roles in technology, luxury, eCommerce, and insurance sectors across three continents.</p>
			<p class="Linkedin" xml:lang="en-GB">LinkedIn: <a href="https://www.linkedin.com/in/manuakumar?lipi=urn%3Ali%3Apage%3Ad_flagship3_profile_view_base_contact_details%3BLnHJuxC6TeeN%2F10vw3szDA%3D%3D">manuakumar</a></p>
			<h2 id="_idParaDest-102" xml:lang="en-GB"><a id="_idTextAnchor176"/>Case Study in Summary</h2>
			<h3 xml:lang="en-GB">Case Study Classification</h3>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB"><span class="No-Break">Segment: B2C</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><span class="No-Break">Industry: Insurance</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><span class="No-Break">Geography: Europe</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Value <a id="_idIndexMarker660"/>creation levers: Growth <span class="No-Break">and retention</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Time to impact: <span class="No-Break">12-18 months</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Regulatory <span class="No-Break">considerations: High</span></li>
			</ul>
			<h3 xml:lang="en-GB">The Business Challenge</h3>
			<p class="P---Regular">A large, multinational <a id="_idIndexMarker661"/>health insurance company with revenue in the low double-digit billions of dollars and operating in a highly regulated industry was suffering from high churn rates across its major countries of operation, implying losses in the current year and future revenues. This intensified pressure on customer acquisition costs as well as inefficiencies in targeting marketing spend. The company was held back by a lack of data and AI solution culture and expertise and high switching costs from a legacy solution that was deeply integrated into actuarial processes like pricing and risk modeling. Any changes required regulatory and <span class="No-Break">stakeholder</span><span class="No-Break"><a id="_idIndexMarker662"/></span><span class="No-Break"> approval.</span></p>
			<p class="P---Regular">The challenge was: </p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB">Develop an effective solution to reduce churn. </li>
				<li class="L---Bullets" xml:lang="en-GB">Prove the value of investments in AI capabilities and deliver a strong, <span class="No-Break">quick ROI.</span></li>
			</ul>
			<h3 xml:lang="en-GB">The Response</h3>
			<p class="P---Regular">Manu was a key member of a new internal team called Customer Lab, created to overcome cultural<a id="_idIndexMarker663"/> resistance and build transformative capability to help the insurer modernize its processes. </p>
			<p class="P---Regular">The team focused on developing an AI-based churn prediction model using an expanded set of 40+ inputs from the insurer’s existing data to identify patterns to predict future churn. These included potential drivers across categories such as product or policy factors (for example, premium increases, discounts, free cover duration), customer profile (age, tenure, and so on), claim patterns and complaints (number of claims, payment delays, and more), as well as signals related to adding dependents to an insurance policy (potentially making certain pricing unattractive for a policyholder). They trialed a variety of AI approaches and finally settled on a Long Short-Term Memory (LSTM) model – a deep <a id="_idIndexMarker664"/>neural network that can use time series information of events to make predictions. </p>
			<p class="P---Regular">The work was done in <span class="No-Break">three phases:</span></p>
			<p class="P---Regular"><strong class="bold">Phase 1: Discovery and building the team</strong>. This phase focused on understanding the core<a id="_idIndexMarker665"/> business challenge and current approach and assembling a new team versed in technologies to be implemented in <span class="No-Break">the solution.</span></p>
			<p class="P---Regular"><strong class="bold">Phase 2: Developing a minimum viable product (MVP)</strong>. This phase involved a 12-week sprint to construct a data-driven MVP to predict customer churn, addressing shortcomings of the <span class="No-Break">existing model.</span></p>
			<p class="P---Regular"><strong class="bold">Phase 3: Driving insights, operationalization, and adoption</strong>. This phase identified key churn drivers and decision matrices and devised customer retention strategies. It enhanced predictive performance accuracy, addressed risk concerns, and promoted <span class="No-Break">widespread adoption.</span></p>
			<p class="P---Regular">This overall approach significantly improved the performance of the model and the ability of the company to proactively identify customers at risk of leaving the business. The model could be applied to 95% of those who had been customers for over a year and<a id="_idIndexMarker666"/> could accurately identify 80% of customers who were likely to churn in the 12 months going forward. </p>
			<h2 id="_idParaDest-103" xml:lang="en-GB"><a id="_idTextAnchor177"/>Case Study in Detail</h2>
			<h3 xml:lang="en-GB">Manu Kumar profile</h3>
			<p class="P---Regular">Manu is currently<a id="_idIndexMarker667"/> the Chief Data Officer at Richemont Group’s YNAP group (Net-a-Porter, YOOX, Mr Porter, and theoutnet.com), focusing on building AI solutions for brands and eCommerce. Previously, he held senior leadership positions in data and AI at an insurance business. Over the past ten years, Manu has focused on data and AI. Prior to this, he worked in product technology roles at companies including Microsoft, Vodafone, and various startups across <span class="No-Break">three continents.</span></p>
			<p class="P---Regular">Manu says his diverse background gives him significant advantages. It leads him to take a “startup-like” approach, where, despite the size of the company, he enjoys getting hands-on, developing the strategy, and then coding alongside teams. In addition, he applies his experience from a variety of industries to relevant situations; for example, his Master’s in Operations Research from Berkeley provided foundational knowledge in techniques like neural networks, which played a valuable role in predicting customer churn in this <span class="No-Break">case study.</span></p>
			<h3 xml:lang="en-GB">The Business Challenge</h3>
			<p class="P---Regular">The insurer<a id="_idIndexMarker668"/> operating in a highly regulated industry was experiencing an expensive customer churn problem with low double-digit churn rates in its scale markets. This impeded revenue trajectory challenged realizing customer value against acquisition cost and exerted further pressure on the marketing and sales organizations. At the time, there was no scalable mechanism in place to proactively identify high-risk customers likely to churn and take steps to <span class="No-Break">prevent this.</span></p>
			<h4 xml:lang="en-GB">Context-dependent issues</h4>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Lack of solution innovation culture and expertise</strong>: The business, aware of data and AI<a id="_idIndexMarker669"/> advances, was seeking to modernize. Until that point, it had been good at sourcing externally built solutions and expertise via advisors but had not yet developed a strong internal innovation culture nor the ability to build tailored solutions for key strategic problems. At this relatively greenfield stage of its analytics and AI capabilities, the company set up a new customer lab team to accelerate its capabilities and hired functional specialists like Manu to prove value. </li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">High switching costs</strong>: The business’s legacy solution was deeply integrated into existing actuarial processes such as pricing, risk modeling, and regulatory reporting. So, you can imagine the amount of resistance to anything new upsetting the apple cart because here I was walking in with a data science background, they thought, “How can he figure out our risks and exposure better than us?” Therefore, there had to be a high level of trust in a new solution. And there were significant switching costs to consider too – it would not only need approval from regulators but also from a wide range of stakeholders, including strategy, legal, compliance, actuarial modeling, <span class="No-Break">and pricing.</span></li>
			</ul>
			<h3 xml:lang="en-GB">Approach</h3>
			<p class="P---Regular"><strong class="bold">MANU</strong>: When<a id="_idIndexMarker670"/> starting a project, I invariably draw on several frameworks, and in this instance referenced <em class="italic">Competing on Analytics</em> and <em class="italic">Analytics at Work</em> by Thomas Davenport and Jeanne Harris. While it might not be entirely relevant to new technologies, Davenport and Harris’s DELTA model offers a valuable framework for understanding and implementing successful analytics programs. The DELTA Plus Model consists of seven <span class="No-Break">key elements:</span></p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB">Data: Quality, integration, and governance <span class="No-Break">of data</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Enterprise orientation: Building an analytical culture and adopting <span class="No-Break">enterprise-wide analytics</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Leadership: Strong executive support <span class="No-Break">for analytics</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Targets: Applying analytics to the right <span class="No-Break">business areas</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Analysts: Hiring and managing <span class="No-Break">analysts effectively</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Tech stack: Having a tech stack that is as cloud-forward <span class="No-Break">as possible</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Analytical techniques: Using appropriate methodologies for <span class="No-Break">data analysis</span></li>
			</ul>
			<p class="P---Regular">I have used this framework across different industries from telco to healthcare and eCommerce. These factors are crucial in planning and implementing successful data science projects. With<a id="_idIndexMarker671"/> this in place, I focus on four key areas, based on my <span class="No-Break">personal experience:</span></p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB">Data capability assessment: Conduct a thorough and honest evaluation of your internal and external <span class="No-Break">data capabilities.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Modernizing models and tools: This is often the low-hanging fruit. By updating your models and tools, you can tap into opportunities that might currently <span class="No-Break">be overlooked.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Change management and transformation: Purchasing software and using algorithms are important but it’s crucial to ensure your team understands the benefits of these tools, how to use them, and the improvements they can bring to their work. This often involves a significant change <span class="No-Break">management effort.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Talent: It might be hard to find but strong data science and data engineering talent is critical, as is having them in an integrated team. I’ve faced issues when data <a id="_idIndexMarker672"/>engineering was not part of the data science team. To deploy multiple models at multiple different points, a very strong data engineering team is <span class="No-Break">absolutely key.</span></li>
			</ul>
			<h4 xml:lang="en-GB">Phase 1: Discovery and building the team</h4>
			<p class="P---Regular"><strong class="bold">MANU</strong>: I began by asking, “What’s<a id="_idIndexMarker673"/> the biggest business problem you have?” The resounding response was customer attrition. Acquiring new customers to replace your old ones is very challenging, so this gave me something meaningful to <span class="No-Break">focus on.</span></p>
			<p class="P---Regular">The first thing I did was go on a discovery mission to figure out how the business was currently addressing this problem. Rather than reinventing the wheel, this meant I could identify a starting point from which we could build. It turned out that the actuarial team had a basic statistical solution in place as part of a wider actuarial software suite that created their prediction tables and pricing structures as well as tracking money in from premiums and out for claims. This was over a decade old and suffered from the impact of model and data drift. In addition, it depended on a dated, “small data” statistical approach that could not leverage the rich signals and datasets available to <span class="No-Break">the company.</span></p>
			<p class="P---Regular">The key problem with this “solution” was that it had low overall accuracy due to two things. Firstly, the model was designed around a narrow set of inputs, using only a few predictor variables (age, gender, postcode, product type, claims, and premiums). It lacked the sophistication to leverage the insurer’s richer customer and claims dataset. Secondly, the simplistic modeling approach of the legacy solution meant it fell short of the business needs. The statistical, multivariate regression model it used didn’t consider the valuable time element of the customer’s journey. Some regression-based approaches can be useful, but in this instance, it was designed more as a simple “risk modeling” tool and not a purpose-built churn prediction tool that could provide predictive insights into specific time horizons. So, the business couldn’t use it to proactively mitigate <span class="No-Break">churn risk.</span></p>
			<p class="P---Regular">In addition to low accuracy, the legacy solution was deeply integrated into existing actuarial processes such as pricing, risk modeling, and regulatory reporting. So, you can imagine the amount of resistance to anything new upsetting the apple cart because here I was walking in with a data science background, they thought, “How can he figure out our risks and exposure better than us?” Therefore, there had to be a high level of trust in a new solution. And there were significant switching costs to consider too – it would not only need approval from regulators but also from a wide range of stakeholders, including strategy, legal, compliance, actuarial modeling, <span class="No-Break">and pricing.</span></p>
			<p class="P---Regular">We lacked data scientists, so I hired externally to bring in the necessary expertise with professionals who understood the technologies we needed to implement. They evaluated our existing algorithms, identified areas for improvement, and upgraded them to <span class="No-Break">enhance </span><span class="No-Break"><a id="_idIndexMarker674"/></span><span class="No-Break">performance.</span></p>
			<h4 xml:lang="en-GB">Phase 2: Developing a minimum viable product (MVP)</h4>
			<p class="P---Regular"><strong class="bold">MANU</strong>: We focused on<a id="_idIndexMarker675"/> developing a data-driven mechanism to predict churn and address key<a id="_idIndexMarker676"/> shortcomings of the existing model. I took in all the underlying datasets and created a new AI-driven system. This system used Long Short-Term Memory (LSTM) networks, which are precursors to ChatGPT. LSTM networks are widely used in deep learning and excel at capturing long-term dependencies, making them ideal for sequence prediction tasks. They can understand and predict patterns in sequential data like time series, text, <span class="No-Break">and speech.</span></p>
			<p class="P---Regular">We outlined a 12-week approach to build and deliver an MVP (minimum viable product) that would demonstrate worth, gain traction, and start to <span class="No-Break">earn trust.</span></p>
			<div>
				<div class="IMG---Figure" id="_idContainer123">
					<img alt="Figure 31.1 – Twelve-week MVP process" src="image/B21583_31_01.jpg"/>
				</div>
			</div>
			<p class="figure-caption">Figure 31.1 – Twelve-week MVP process</p>
			<p class="P---Regular"><strong class="bold">Exploring datasets and inputs</strong>: As a first step, we undertook a scan of potential datasets that would help us understand the customer’s experience and likelihood of churn. Instead of the initial list of limited variables, we expanded the dataset to include more than 40 variables, a volume the previous in-house system couldn’t manage, with more signals than before. The original software, which had not been updated in a decade, exhibited significant data and model drift. This drift is a common issue in machine learning models where the statistical properties of the target variable, which the model is trying to predict, change over time in unforeseen ways. This can lead to a decrease in <span class="No-Break">model accuracy.</span></p>
			<p class="P---Regular">The following table shows some of the categories we worked across. This richer dataset helped to significantly improve the model and provided extensive insights into the business. Given the sensitive nature of the data, all datasets underwent significant review and detailed processes, with the oversight of risk and regulatory teams to remove <a id="_idIndexMarker677"/>Personally<a id="_idIndexMarker678"/> Identifiable Information (PII) and maintain high levels <span class="No-Break">of security.</span></p>
			<table class="No-Table-Style" id="table001">
				<colgroup>
					<col/>
					<col/>
					<col/>
					<col/>
					<col/>
				</colgroup>
				<tbody>
					<tr class="No-Table-Style">
						<td class="No-Table-Style T---Header">
							<p class="T---Heading">POLICY OR PRODUCT <span class="No-Break">FACTORS</span></p>
						</td>
						<td class="No-Table-Style T---Header">
							<p class="T---Heading">CUSTOMER PROFILE <span class="No-Break">FACTORS</span></p>
						</td>
						<td class="No-Table-Style T---Header">
							<p class="T---Heading">DEPENDENT RELATED <span class="No-Break">FACTORS</span></p>
						</td>
						<td class="No-Table-Style T---Header">
							<p class="T---Heading">CLAIM AND COMPLAINT <span class="No-Break">FACTORS</span></p>
						</td>
						<td class="No-Table-Style T---Header">
							<p class="T---Heading">TIME <span class="No-Break">SERIES</span></p>
						</td>
					</tr>
					<tr class="No-Table-Style">
						<td class="No-Table-Style T---Body">
							<p class="T---Regular">Number of products sold <span class="No-Break">to customer</span></p>
							<p class="T---Regular">Current premiums <span class="No-Break">and excess</span></p>
							<p class="T---Regular">Historical premium <span class="No-Break">changes</span></p>
							<p class="T---Regular">Levels <span class="No-Break">of discounting</span></p>
							<p class="T---Regular">Free premium periods <span class="No-Break">offered</span></p>
							<p class="T---Regular">Policy renewal <span class="No-Break">timeframe</span></p>
							<p class="T---Regular">Next payment <span class="No-Break">timeframe</span></p>
						</td>
						<td class="No-Table-Style T---Body">
							<p class="T---Regular"><span class="No-Break">Age</span></p>
							<p class="T---Regular"><span class="No-Break">Gender</span></p>
							<p class="T---Regular">Premium relative to other customers (<span class="No-Break">deciles)</span></p>
							<p class="T---Regular">Location (<span class="No-Break">address)</span></p>
							<p class="T---Regular">Household <span class="No-Break">status</span></p>
						</td>
						<td class="No-Table-Style T---Body">
							<p class="T---Regular">Number of dependents (partners, children, and <span class="No-Break">so on)</span></p>
							<p class="T---Regular">Claims made by dependents – <span class="No-Break">value</span></p>
							<p class="T---Regular">Claims made by dependents – <span class="No-Break">details</span></p>
							<p class="T---Regular">Claims made by dependents – <span class="No-Break">count</span></p>
						</td>
						<td class="No-Table-Style T---Body">
							<p class="T---Regular">Invoices and items generated </p>
							<p class="T---Regular"><span class="No-Break">Amount claimed</span></p>
							<p class="T---Regular">Details <span class="No-Break">of claims</span></p>
							<p class="T---Regular">Number <span class="No-Break">of claims</span></p>
							<p class="T---Regular">Timeframe of claims processing </p>
							<p class="T---Regular">Number of days in payments <span class="No-Break">delay</span></p>
							<p class="T---Regular">Number of customer experience issues (for example, <span class="No-Break">complaints)</span></p>
						</td>
						<td class="No-Table-Style T---Body">
							<p class="T---Regular">Time-stamp of claims and customer support </p>
							<p class="T---Regular"><span class="No-Break">Sequence</span></p>
						</td>
					</tr>
				</tbody>
			</table>
			<p class="figure-caption">Table 31.1 – Categories integrated to enrich the dataset</p>
			<p class="P---Regular"><strong class="bold">Preparation: validating hypotheses and creating insights</strong>: Building a clean, consolidated dataset gave us access to key insights for the business to interrogate the data and drive proactive retention measures. This initial approach got the dataset into the hands of the business early and informed dashboards that a variety of teams could access and use to understand customers and behaviors. Examples of the analytics include: </p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB">Volume of total customers and churners by business line, such as personal versus company <span class="No-Break">insurance schemes.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Mapping churners and customers to overall premiums. </li>
				<li class="L---Bullets" xml:lang="en-GB">Average <span class="No-Break">customer tenure.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">Where in the journey stage customers churn. We found that 60%+ of applicants lapsed at the time of policy renewal, and 50%+ contribute 80% of premiums. </li>
				<li class="L---Bullets" xml:lang="en-GB">Compare the distribution of customers and average premiums against churn rates. This analysis helps identify patterns or trends, such as whether higher premiums lead to higher rates or the opposite, or if certain groups of customers are more likely to churn than others. It serves to make strategic decisions <a id="_idIndexMarker679"/>about <a id="_idIndexMarker680"/>pricing and <span class="No-Break">customer retention.</span></li>
			</ul>
			<p class="P---Regular"><strong class="bold">Developing churn prediction models</strong>: We developed a sample of data to train our models. We used a simple funnel imager to help the business understand the chosen population. </p>
			<h5>Evaluating modeling techniques for <span class="No-Break">churn prediction</span></h5>
			<p class="P---Regular">With deep learning, I could go much further. We evaluated a number of AI techniques to model churn prediction. These included the Cox proportional hazards survival model, Generalized Linear Models (GLMs), and <span class="No-Break">gradient boosting.</span></p>
			<table class="T---Table" id="table002">
				<colgroup>
					<col/>
					<col/>
					<col/>
					<col/>
				</colgroup>
				<thead>
					<tr class="T---Table">
						<td class="T---Table T---Header T---Header">
							<p class="T---Heading"><span class="No-Break">APPROACH</span></p>
						</td>
						<td class="T---Table T---Header T---Header">
							<p class="T---Heading"><span class="No-Break">DESCRIPTION</span></p>
						</td>
						<td class="T---Table T---Header T---Header">
							<p class="T---Heading"><span class="No-Break">ADVANTAGES</span></p>
						</td>
						<td class="T---Table T---Header T---Header">
							<p class="T---Heading"><span class="No-Break">DISADVANTAGES</span></p>
						</td>
					</tr>
				</thead>
				<tbody>
					<tr class="T---Table">
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Cox Proportional Hazards Survival <span class="No-Break">Analysis</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Developed by David Cox in 1972, this is a <span class="No-Break">regression model.</span></p>
							<p class="T---Regular">This model is particularly useful because it works for both quantitative predictor variables and categorical variables. </p>
							<p class="T---Regular">Often used in churn prediction to predict time until an event (a lapse, in this <span class="No-Break">instance) occurs.</span></p>
							<p class="T---Regular">Commonly used in medical research. </p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Capable of handling data with varying time intervals allowing for flexible <span class="No-Break">analysis.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Assumes that the risk (hazard) remains constant over time, which is not the case in forecasting lapses. </p>
							<p class="T---Regular">Results can be complex for non-technical users to interpret. </p>
						</td>
					</tr>
					<tr class="T---Table">
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Generalized Linear Models (<span class="No-Break">GLMs)</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Developed by John Nelder and Robert Wedderburn as a way of unifying various statistical models, including linear regression, logistic regression, and Poisson regression. </p>
							<p class="T---Regular">Serves as a flexible extension of ordinary linear regression models. </p>
							<p class="T---Regular">Can handle both quantitative and categorical predictor variables, for example, count, binary outcomes, <span class="No-Break">or rates.</span></p>
							<p class="T---Regular">Used for applications such as predictive maintenance, insurance <span class="No-Break">conversion modeling.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Ability to measure effect of input variables <span class="No-Break">on churn.</span></p>
							<p class="T---Regular">Offers flexibility to handle different types of data, including binary, count, and continuous <span class="No-Break">outcomes.</span></p>
							<p class="T---Regular">Results are easier <span class="No-Break">to interpret.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Presumes a linear relationship between variables, which was not the case in predicting <span class="No-Break">lapses.</span></p>
							<p class="T---Regular">Prone to overfitting in <span class="No-Break">specific segments.</span></p>
							<p class="T---Regular">Less effective in capturing complex, non-linear relationships <span class="No-Break">in data.</span></p>
						</td>
					</tr>
					<tr class="T---Table">
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Gradient <span class="No-Break">Boosting</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Developed by several contributors: Leo Breiman, Jerome H. Friedman, Llew Manson, Jonathan Baxter, and <span class="No-Break">Marcus Frean.</span></p>
							<p class="T---Regular">Machine learning technique used for regression and <span class="No-Break">classification tasks.</span></p>
							<p class="T---Regular">Creates a prediction model in the form of an ensemble of weak prediction models, typically simple <span class="No-Break">decision trees.</span></p>
							<p class="T---Regular">These models are added sequentially, each one attempting to correct the errors of its predecessor. </p>
							<p class="T---Regular">The final prediction is made by summing up the predictions of all weak learners, each weighted based on <span class="No-Break">its performance.</span></p>
							<p class="T---Regular">Effective in churn prediction problems and <span class="No-Break">risk modeling.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Ability to learn about relationship between independent variables on the dependent ones. </p>
							<p class="T---Regular">Churn datasets are imbalanced by default (most customers stay); helped deal with bias in the dataset towards retained <span class="No-Break">customers.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Was prone to overfitting. </p>
							<p class="T---Regular">Challenging to use for learning lifetime behaviors and balancing weight of events <span class="No-Break">over time.</span></p>
							<p class="T---Regular">Can be computationally <span class="No-Break">intensive.</span></p>
						</td>
					</tr>
					<tr class="T---Table">
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Two-stage LSTM (Long Short-Term <span class="No-Break">Memory)</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">LSTM networks are a type of recurrent neural network that excels in learning from sequences of data. </p>
							<p class="T---Regular">Adept at applications involving time-series data. </p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Effective with sequential and time-based data and able to learn from both events and when they happened – making them valuable for complex churn problems. </p>
							<p class="T---Regular">Ability to use rich transactional data at an <span class="No-Break">account level.</span></p>
						</td>
						<td class="T---Table T---Body T---Body">
							<p class="T---Regular">Computationally <span class="No-Break">resource-intensive.</span></p>
							<p class="T---Regular">Complex to understand and explain in terms of specific variables to focus on and action <span class="No-Break">to take.</span></p>
						</td>
					</tr>
				</tbody>
			</table>
			<p class="figure-caption">Table 31.2 – AI techniques evaluated for churn</p>
			<p class="P---Regular">The LSTM model was our final choice. It offered us the advantage of identifying patterns not only from the “events” in the expanded set of variables but also from the timeline series. This included factors such as the timing of the event, its frequency, and its sequence. </p>
			<p class="P---Regular">Transitioning from one solution to another, particularly in the realm of time series analysis, can make a significant difference. Using the most advanced time series solutions available at any given time can greatly enhance the accuracy and efficiency of <span class="No-Break">data analysis.</span></p>
			<p class="P---Regular">One of the key <a id="_idIndexMarker681"/>advantages<a id="_idIndexMarker682"/> of modern methods is the ability to perform feature engineering. This process involves creating new input features for machine learning from existing variables, which can improve model performance. However, if you opt for a commodity solution that merely organizes your data in a certain way, using a few variables, you may miss out on the full potential of your data analysis. This is especially true if you don’t have the time to create<a id="_idIndexMarker683"/> a Business Analytics Solution (BAS) book – a comprehensive document detailing your data analysis processes and findings. Therefore, investing time in feature engineering and creating a BAS book can help you leverage the full potential of your data and yield more accurate and <span class="No-Break">insightful results.</span></p>
			<p class="P---Regular">To find the optimal solution, I conducted thorough performance checks and model explanations. I explained and likened this process to a Google search, where the interaction between a company and its customers is through language. Just as you type a search word on Google and it predicts what you might be looking for, businesses can use similar predictive models to anticipate a customer’s next action, such as a churn event or a product upgrade/downgrade. The most successful companies have implemented this architecture for highly <span class="No-Break">visible products.</span></p>
			<p class="P---Regular">The beauty of using LSTMs lies in their ability to capture and analyze temporal patterns in data. We used LSTMs to <a id="_idIndexMarker684"/>calculate Customer Lifetime Value (CLTV) and churn rates, creating cohorts based on these values. This approach allows us to factor in acquisition costs in a <span class="No-Break">detailed manner.</span></p>
			<p class="P---Regular">We can examine each customer individually and decide on the most effective intervention. For instance, for a customer who is highly likely to churn but has a high lifetime value, we might decide to have an insurance agent call them directly. This is an expensive intervention, so we would only use it for a small list of customers. This strategy ensures that resources are allocated efficiently, targeting customers who are most likely to bring value to the company in the <span class="No-Break">long run.</span></p>
			<p class="P---Regular">The CLTV model we developed was more advanced than any previous system. By using LSTMs, we were able to leverage past transaction data for feature engineering and model development. Of course, all data was anonymized in compliance with the General Data Protection Regulation (GDPR). This <a id="_idIndexMarker685"/>approach allowed us to create distinct predictions for different cohorts, particularly regarding their likely claims as a group. The necessary data was readily available from past <span class="No-Break">claim submissions.</span></p>
			<p class="P---Regular">The importance of time series analysis in this context cannot be overstated. It allows us to layer events into a timeline, providing a more comprehensive view of customer behavior. </p>
			<p class="P---Regular">The maturity of LSTMs played a crucial role in our success because they allowed us to fully unlock the potential of our datasets. The solution was tailor-made for our needs, which<a id="_idIndexMarker686"/> significantly<a id="_idIndexMarker687"/> enhanced its effectiveness. By leveraging the capabilities of LSTMs, we were able to extract valuable insights from our data and create robust <span class="No-Break">predictive models.</span></p>
			<h4 xml:lang="en-GB">Phase 3: Driving insights, operationalization, and adoption</h4>
			<p class="P---Regular"><strong class="bold">MANU</strong>: Our analysis <a id="_idIndexMarker688"/>provided<a id="_idIndexMarker689"/> valuable<a id="_idIndexMarker690"/> insights into churn drivers and potential interventions for customer retention. For instance, accounts with promotional incentives exhibited a lower <span class="No-Break">churn propensity.</span></p>
			<p class="P---Regular">In hindsight, getting a step change in predictive performance was the easy part because there was a good response to the results. However, the real challenge lay in overcoming the perception of risk, explaining the analytics and benefits to the broader business, and <span class="No-Break">fostering adoption.</span></p>
			<p class="P---Regular">I often advise Chief Data Officers that if the reports, tools, or decision engines they create are not being used or implemented, then nothing they did matters. It’s not just about creating the system; it’s about making sure people are using it and processes are updated. </p>
			<p class="P---Regular">We implemented several strategies to <span class="No-Break">drive adoption:</span></p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Showing the value</strong>: We emphasized the value of improved prediction accuracy to the business, particularly in terms of customer retention through specific interventions. We quantified this value in terms of Return on Investment (ROI), taking into account the cost of interventions and <span class="No-Break">potential gains.</span></li>
			</ul>
			<p class="L---Regular" xml:lang="en-GB">To provide a more tangible understanding of the potential benefits and trade-offs of different <a id="_idIndexMarker691"/>customer <a id="_idIndexMarker692"/>retention strategies, we <a id="_idIndexMarker693"/>devised “trade-off matrices.” These matrices demonstrated the viability of specific initiatives, such as offering free cover, based on their adoption and retention percentages. This approach enabled us to effectively communicate the potential advantages and compromises associated with <span class="No-Break">each strategy.</span></p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Translating</strong>: We realized that there was a need to translate our work into clearer and more understandable terms for people unfamiliar with AI and bring the benefits to life through analogies and <span class="No-Break">live events.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Explaining prediction</strong>: People tend to think of prediction systems in binary terms, as simply being accurate or not. We spent considerable time explaining the confusion matrix in detail – false positives, false negatives, and so on – and what the value of reducing false negatives would be for the business. We used the analogy of COVID testing (topical at that time) and virus spread to explain the importance of precision to avoid false negatives. A false negative in COVID testing meant you didn’t pick someone who had the virus, and they went around spreading it, whereas a false positive might mean a person just got one more booster. With this explainer, people really got the whole concept of false negative and positive scores, and how the former was potentially <span class="No-Break">more serious.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Audience-based access</strong>: Not all information can be shared with everyone, so our approach to accessing data is to grant it based on privilege levels. For instance, regulators and actuaries have access to more comprehensive information due to the nature of their roles. This ensures that sensitive data is only accessible to those who need it for their specific functions, thereby maintaining data security <span class="No-Break">and privacy.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Actuary copilot</strong>: To address the pushback from various teams who might have felt threatened by this technology, I explained how the AI-driven solution could “augment” actuaries, as an AI copilot would make them more capable rather than replacing them. Because this one architecture could deliver <a id="_idIndexMarker694"/>better <a id="_idIndexMarker695"/>insights, it would help the teams <a id="_idIndexMarker696"/>price risk better and prevent the business from leaking value (such as leaking 1-2% fewer customers per year). Plus, it could solve more problems by layering in customer lifetime value, to design campaigns, and <span class="No-Break">so on.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Industry examples</strong>: By comparing the predictive nature of the architecture to how Google predicts search phrases, gave the teams reason to believe and build trust. Showing how one of the world’s leading companies has adopted such a framework demonstrated how their business could forecast whether a customer might upgrade, downgrade, or discontinue a product or service. </li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Getting close-up</strong>: To drive change management, the team and I initiated live events with lots of webinars, roadshows, presentations, and different Executive Committee forums and so on to get people <span class="No-Break">on board.</span></li>
			</ul>
			<h4 xml:lang="en-GB">Results and Impact</h4>
			<p class="P---Regular"><strong class="bold">MANU</strong>: The implementation <a id="_idIndexMarker697"/>of an AI-driven solution dramatically transformed the business operations. It achieved extensive coverage and applicability across the customer base, significantly outperforming the previous model and enhancing revenue generation. Moreover, it optimized internal processes, paving the way for future <span class="No-Break">technological advancements.</span></p>
			<p class="P---Regular"><span class="No-Break">Impacts included:</span></p>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB">The AI solution covered over 95% of the business and was applicable to most customers with a history exceeding <span class="No-Break">one year.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">It could accurately identify 80% of customers likely to churn in the forthcoming <span class="No-Break">12 months.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">An eightfold performance improvement over the previous regression-based solution and yielded tens of millions in additional revenue over the period. </li>
				<li class="L---Bullets" xml:lang="en-GB">The model streamlined multiple internal processes and enabled targeted, cost-effective retention campaigns based on finely segmented churn <span class="No-Break">risk scoring.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">It unlocked additional use cases like predicting Customer Lifetime Value (CLV) and personalized pricing based <span class="No-Break">on risk.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">The MVP’s success gained approval for full-scale adoption to the company’s <span class="No-Break">largest market.</span></li>
				<li class="L---Bullets" xml:lang="en-GB">The project established foundational data pipelines and AI deployment processes for <span class="No-Break">future initiatives.</span></li>
			</ul>
			<h3 xml:lang="en-GB">Key Learnings</h3>
			<ul>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Baseline technical capability</strong>: Before introducing a new AI solution, assess the critical business problem to solve as well as the current systems (architecture, integration, performance, and scalability). This helps benchmark progress and identify opportunities, and risks. Understand foundational tech components to determine where to invest for risk mitigation. An important aspect of the assessment is to understand user workflows and the industry-standard technology that they rely on for critical processes. With due consideration, the business can integrate industry-standard solutions <span class="No-Break">where appropriate.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Opportunities for quick wins</strong>: Often, the initial outcome of a technical assessment identifies opportunities for quick wins. Even in an organization burdened with legacy systems and a suboptimal architecture, an AI solution can work effectively with <span class="No-Break">minimal integration.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Identify in-house gaps in capabilities</strong>: Assessing the capabilities of the internal team is also crucial at the start. Rapidly augment capabilities through targeted hires and strategic partnerships with <span class="No-Break">specialist firms.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Data expansion</strong>: Leveraging a broader set of data inputs can significantly improve the accuracy of predictive models and provide a more comprehensive understanding of customer behavior and potential churn indicators. </li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Algorithm selection</strong>: The primary goal should be to solve the problem at hand, choosing and combining algorithms based on their suitability for the problem, not convenience. Understand the pros and cons of each method or algorithm. Update select algorithms and tools to offer immediate improvements in the development of <span class="No-Break">AI solutions.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Minimum Viable Product (MVP)</strong>: Hands-on rapid development of an MVP provides quick progress within a typical 12-week delivery timeline. This includes bottom-up use case preparation, design and UI, build, UAT/QA, <span class="No-Break">and deployment.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Trade-off matrices</strong>: Effectively explaining precision versus accuracy and associated costs enables informed trade-off discussions. Developing “trade-off matrices” can provide a tangible understanding of the potential benefits and trade-offs of different customer <span class="No-Break">retention strategies.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Buy-in from skilled employees</strong>: It’s crucial to ensure the work is understandable to colleagues less familiar with AI. Integrating the AI-driven solution as an “augmentation” to skilled employees for specific process steps can overcome resistance. Progress can be slower than anticipated as it can take time to build trust and buy-in. An iterative and user/customer-led approach that integrates feedback is essential to the <span class="No-Break">ongoing process.</span></li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Strategies for solution adoption</strong>: Using various strategies, including analogies, other industry examples, and live events, can drive change management and ensure the successful adoption of the new solution into business workflows. Setting up a separate team within the business to accelerate the solution by building expertise and a change-oriented mindset can enable the core business operations to continue uninterrupted. </li>
				<li class="L---Bullets" xml:lang="en-GB"><strong class="bold">Data security and access controls</strong>: When working with sensitive data, ensuring security<a id="_idIndexMarker698"/> and compliance is vital, especially when handling Personally Identifiable Information (PII). Implementing an audience-based access control system can ensure only authorized individuals have access to sensitive data. This ensures ongoing data security <span class="No-Break">and privacy.</span></li>
			</ul>
		</div>
		<div>
			<div id="_idContainer125">
			</div>
		</div>
		<div>
			<div id="_idContainer126">
			</div>
		</div>
		<div>
			<div id="_idContainer127">
			</div>
		</div>
		<div>
			<div id="_idContainer128">
			</div>
		</div>
	</body>
</html>
    """

    analyzer = HTMLAnalyzer()
    analyzer.analyze_html(test_html)


if __name__ == "__main__":
    main()
