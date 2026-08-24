const luaparse = require('./luaparse.js');

const parse = luaparse.parse;

const isNumericTree = node =>
    node.type === "NumericLiteral" ||
    (
        node.type === "BinaryExpression" &&
        isNumericTree(node.left) &&
        isNumericTree(node.right)
    ) ||
    (
        node.type == "UnaryExpression" &&
        node.operator == "-" &&
        isNumericTree(node.argument)
    )

const getNum = (expr) =>
    expr.type == "NumericLiteral" ? expr.value : expr.type == "UnaryExpression" ? -getNum(expr.argument) : solveMath(expr.left, expr.operator, expr.right);

const areNumbers = (lhs, rhs) =>
    isNumericTree(lhs) && isNumericTree(rhs)

const solveMath = (leftExpr, operator, rightExpr) => { // print((2 + 3) * (4 + (6 / 2)))
    if (!operator)
        return leftExpr.type == "NumericLiteral" ? leftExpr.value : solveMath(leftExpr.left, leftExpr.operator, leftExpr.right)

    let left, right;
    if (areNumbers(leftExpr, rightExpr)) {
        const index = '+-/*//\^'.indexOf(operator)

        left = getNum(leftExpr), right = getNum(rightExpr);

        if (left == null || right == null) // wat....
            return;

        let r;
        if (index == 0) // +
            r = left + right;
        else if (index == 1)
            r = left - right
        else if (index == 2)
            r = left / right
        else if (index == 3)
            r = left * right
        else if (index == 4)
            r = Math.floor(left / right)
        else if (index == 6)
            r = Math.pow(left, right)

        return r;
    }

    if (leftExpr.type == "BinaryExpression")
        if (!areNumbers(leftExpr.left, leftExpr.right))
            return;
        else
            left = solveMath(leftExpr.left, leftExpr.operator, leftExpr.right)
    if (rightExpr.type == "BinaryExpression")
        if (!areNumbers(rightExpr.left, rightExpr.right))
            return;
        else
            right = solveMath(rightExpr.left, rightExpr.operator, rightExpr.right)

    if (left == null || right == null)
        return;

    return solveMath(left, operator, right)
}

var PRECEDENCE = {
    'or': 1,
    'and': 2,
    '<': 3,
    '>': 3,
    '<=': 3,
    '>=': 3,
    '~=': 3,
    '==': 3,
    '..': 5,
    '+': 6,
    '-': 6, // binary -
    '*': 7,
    '/': 7,
    '%': 7,
    'unarynot': 8,
    'unary#': 8,
    'unary-': 8, // unary -
    '^': 10
};

const isNan = (a) => !(a < 0) && !(a > 0) && a != 0

var each = function(array, fn) {
    var index = -1;
    var length = array.length;
    var max = length - 1;
    while (++index < length) {
        fn(array[index], index < max);
    }
};

var hasOwnProperty = {}.hasOwnProperty;
var extend = function(destination, source) {
    var key;
    if (source) {
        for (key in source) {
            if (hasOwnProperty.call(source, key)) {
                destination[key] = source[key];
            }
        }
    }
    return destination;
};

/*--------------------------------------------------------------------------*/

const joinStatements = (a, b, separator) => a + (separator || " ") + b;
const formatBase = function(base, indent = 0) {
    var result = '';
    var type = base.type;
    var needsParens = base.inParens || (
        type == 'BinaryExpression' ||
        type == 'FunctionDeclaration' ||
        type == 'TableConstructorExpression' ||
        type == 'LogicalExpression' ||
        type == 'StringLiteral' ||
        type == "VarargLiteral"
    );
    if (needsParens) {
        result += '(';
    }
    result += formatExpression(base, null, indent);
    if (needsParens) {
        result += ')';
    }
    return result;
};

const LUA_TO_JS = (luaStr) =>
    /*luaStr.replace(/\\(\d{1,3})|\\(.)|[\uD800-\uDBFF][\uDC00-\uDFFF]|[\s\S]/g, (match, n, escaped) => {
        if (n !== undefined) {
            const num = Number(n)
            if (num >= 32 && num <= 126) return String.fromCharCode(num)
            return `\\u{${num.toString(16)}}`
        }
        if (escaped !== undefined) {
            if (escaped === '\\') return '\\\\'
            if (escaped === 'a')  return '\\u{7}'
            return '\\' + escaped
        }
        const code = match.codePointAt(0)
        if (code === 92) return '\\\\'
        if (code < 32 || code > 126) return `\\u{${code.toString(16)}}`
        return match
    })*/
   luaStr.replace(/\\(\d{1,3})/, (_, code) => {
        const actual = Number(code)
        if (actual < 32) return "\\" + actual
        return String.fromCharCode(actual)
   })

var formatExpression = function(expression, options, indent = 0) {
    options = extend({
        'precedence': 0
    }, options);

    var result = '';
    var currentPrecedence;
    var associativity;
    var operator;

	const tab = "    ".repeat(indent)
	const nextTab = "    ".repeat(indent + 1)

    var expressionType = expression.type;

    if (expressionType == 'Identifier') {

        result = expression.name;

    } else if (
        expressionType == 'StringLiteral' ||
        expressionType == 'NumericLiteral' ||
        expressionType == 'BooleanLiteral' ||
        expressionType == 'NilLiteral' ||
        expressionType == 'VarargLiteral'
    ) {
        if (expressionType == "StringLiteral") {
            //console.log(expression)
            //if (expression.value) return `"${LUA_TO_JS(expression.value).replace(/"/g, '\\"')}"`//console.log(expression.value)
            //return LUA_TO_JS(expression.raw)

            const raw = expression.raw

            return raw.replace(/./g, (m) => {
                const code = m.charCodeAt(0)

                //if (code < 32 || code > 127) return "\\" + code
                if (code < 32) return "\\" + code
                return m
            }).replace(/\r/, "\\r")
        }
        result = expression.raw;

    } else if (
        expressionType == 'LogicalExpression' ||
        expressionType == 'BinaryExpression'
    ) { // binaryop
        // If an expression with precedence x
        // contains an expression with precedence < x,
        // the inner expression must be wrapped in parens.
        operator = expression.operator;
        currentPrecedence = PRECEDENCE[operator];
        associativity = 'left';

        const solved = solveMath(expression.left, operator, expression.right)

        if (solved != undefined && solved != null) {
            if (solved == Infinity)
                return "math.huge"
            if (isNan(solved))
                return "0 / 0"
            return '' + solved; // tostring i guess
        }

        result = formatExpression(expression.left, {
            'precedence': currentPrecedence,
            'direction': 'left',
            'parent': operator
        }, indent);
        result = joinStatements(result, operator);
        result = joinStatements(result, formatExpression(expression.right, {
            'precedence': currentPrecedence,
            'direction': 'right',
            'parent': operator
        }, indent));

        if (operator == '^' || operator == '..') {
            associativity = "right";
        }

        if (
            currentPrecedence < options.precedence ||
            (
                currentPrecedence == options.precedence &&
                associativity != options.direction &&
                options.parent != '+' &&
                !(options.parent == '*' && (operator == '/' || operator == '*'))
            )
        ) {
            // The most simple case here is that of
            // protecting the parentheses on the RHS of
            // `1 - (2 - 3)` but deleting them from `(1 - 2) - 3`.
            // This is generally the right thing to do. The
            // semantics of `+` are special however: `1 + (2 - 3)`
            // == `1 + 2 - 3`. `-` and `+` are the only two operators
            // who share their precedence level. `*` also can
            // commute in such a way with `/`, but not with `%`
            // (all three share a precedence). So we test for
            // all of these conditions and avoid emitting
            // parentheses in the cases where we don’t have to.
            result = '(' + result + ')';
        }

    } else if (expressionType == 'UnaryExpression') {

        operator = expression.operator;
        currentPrecedence = PRECEDENCE['unary' + operator];

        result = operator + (operator == "not" ? " " : "") + formatExpression(expression.argument, { 'precedence': currentPrecedence }, indent)

        if (
            currentPrecedence < options.precedence &&
            // In principle, we should parenthesize the RHS of an
            // expression like `3^-2`, because `^` has higher precedence
            // than unary `-` according to the manual. But that is
            // misleading on the RHS of `^`, since the parser will
            // always try to find a unary operator regardless of
            // precedence.
            !(
                (options.parent == '^') &&
                options.direction == 'right'
            )
        ) {
            result = '(' + result + ')';
        }
    } else if (expressionType == 'CallExpression') {

        result = formatBase(expression.base, indent) + '(';

		const args = []

        each(expression.arguments, (argument) => args.push(formatExpression(argument, null, indent)));

		result += args.join(", ")
        result += ')';

    } else if (expressionType == 'TableCallExpression') {

        result = formatExpression(expression.base, null, indent + 1) +
            formatExpression(expression.arguments, null, indent);

    } else if (expressionType == 'StringCallExpression') {
        const argument = expression.base
        
        result = formatExpression(argument, null, indent) +
            formatExpression(expression.argument, null, indent);

    } else if (expressionType == 'IndexExpression') { // a[b]

        result = formatBase(expression.base, indent)
        if (expression.base.type == "VarargLiteral")
            result = `({${result}})`
        result = expression.base.type == "TableConstructorExpression" ? `(${result})` : result
        result += '[' +
            formatExpression(expression.index, null, indent) + ']';

    } else if (expressionType == 'MemberExpression') { // a.b | a:b
        result = formatBase(expression.base, indent) + expression.indexer + 
            formatExpression(expression.identifier);

    } else if (expressionType == 'FunctionDeclaration') {
        result = 'function(';
        if (expression.parameters.length) {
            each(expression.parameters, function(parameter, needsComma) {
                // `Identifier`s have a `name`, `VarargLiteral`s have a `value`
                result += parameter.name || parameter.value;
                if (needsComma) result += ', ';
            });
        }

        result += ')';
        result = joinStatements(result, formatStatementList(expression.body, indent + 1), "\n"); // the body
        result = joinStatements(result, "\n" + tab + "end");

    } else if (expressionType == 'TableConstructorExpression') {

        if (expression.fields.length == 1 && expression.fields[0].value?.type == "VarargLiteral")
            return "{...}"

        const stuff = []

        each(expression.fields, function(field) {
            if (field.type == 'TableKey') { // [1] = 123
                stuff.push('[' + formatExpression(field.key, null, indent) + '] = ' +
                    formatExpression(field.value, null, indent + 1))
            } else if (field.type == 'TableValue') { // 123, array type
                stuff.push(formatExpression(field.value, null, indent + 1));
            } else { // at this point, `field.type == 'TableKeyString'` (a = 123)
                stuff.push(formatExpression(field.key) + ' = ' + formatExpression(field.value, null, indent + 1))
            }
        });

        //result += (expression.fields.length > 0 ? newline + tab : "") + '}';
        const line = "\n" + nextTab
        result = stuff.length == 0 ? "{}" : `{${line}${stuff.join("," + line)}\n${tab}}`
    } else if (expressionType == "InterpolatedStringExpression") {
        for (let x of expression.parts)
            result += (x.type == "StringLiteral" ? x.value : `{${formatExpression(x)}}`)
        result = `\`${result}\``
    } else {
        throw TypeError('Unknown expression type: `' + expressionType + '`');
    }

    if (expression.inParens) {
        return `(${result})`
    }

    return result;
};

var formatStatementList = function(body, indent = 0) {
    const stats = []
    if (!body) throw new Error("no body given to formatStatementList!")
    each(body, stat => {
        if (!stat || !stat.type) return

        stats.push(formatStatement(stat, indent));
    })

	const tab = "    ".repeat(indent)
	const joined = stats.join(";\n" + tab)
		return tab + joined + (stats.length > 0 && joined.substring(joined.length - 1) != ";" ? ";" : "")
};

var formatStatement = function(statement, indent=0) {
    if (!statement || !statement.type) return '' // null object or something

    var result = '';
    var statementType = statement.type;

	const tab = "    ".repeat(indent)
	const newline = "\n" + tab
	const end = newline + "end"

    if (statementType == 'AssignmentStatement') {
        // left-hand side
        each(statement.variables, function(variable, needsComma) {
            result += formatExpression(variable, null, indent);
            if (needsComma) result += ', ';
        });

        // right-hand side
        result += ' = ';
        each(statement.init, function(init, needsComma) {
            result += formatExpression(init, null, indent);
            if (needsComma) result += ', ';
        });

    } else if (statementType == 'LocalStatement') {

        result = 'local ';

        // left-hand side
        each(statement.variables, function(variable, needsComma) {
            // Variables in a `LocalStatement` are always local, duh
            result += variable.name;
            if (needsComma) {
                result += ', ';
            }
        });

        // right-hand side
        if (statement.init.length) {
            result += ' = ';
            each(statement.init, function(init, needsComma) {
                result += formatExpression(init, null, indent);
                if (needsComma) {
                    result += ', ';
                }
            });
        }

    } else if (statementType == 'CallStatement') {

        result = formatExpression(statement.expression, null, indent);

    } else if (statementType == 'IfStatement') {
        result = joinStatements(
            'if',
            formatExpression(statement.clauses[0].condition, null, indent)
        );
        result = joinStatements(result, 'then');
        const clause = statement.clauses[0].body
        result = joinStatements(
            result,
            formatStatementList(clause, indent + 1),
			clause.length ? "\n" : "" // dont waste a newline
        );
        each(statement.clauses.slice(1), function(clause) {
            if (clause.condition) {
                result = joinStatements(result, 'elseif', newline);
                result = joinStatements(result, formatExpression(clause.condition, null, indent - 1), null, indent);
                result = joinStatements(result, 'then');
            } else {
                result = joinStatements(result, 'else', newline);
            }
            result = joinStatements(result, formatStatementList(clause.body, indent + 1), "\n");
        });
        result = joinStatements(result, end);

    } else if (statementType == 'WhileStatement') {

        result = joinStatements('while', formatExpression(statement.condition, null, indent));
        result = joinStatements(result, 'do');
        if (statement.body.length != 0) {
            result = joinStatements(result, formatStatementList(statement.body, indent + 1), "\n");
            result = joinStatements(result, end);
        } else
            result = result + " end"

    } else if (statementType == 'DoStatement') {

        result = `do\n` + formatStatementList(statement.body, indent + 1);
        result = joinStatements(result, end);
    } else if (statementType == 'Chunk') {

        result = formatStatementList(statement.body, indent)

    } else if (statementType == 'ReturnStatement') {

        result = 'return';

        each(statement.arguments, function(argument, needsComma) {
            result = joinStatements(result, formatExpression(argument, null, indent));
            if (needsComma) result += ', ';
        });

    } else if (statementType == 'BreakStatement') {
        result = 'break';
    } else if (statementType == "ContinueStatement") {
        result = "continue"
    } else if (statementType == "CompoundAssignmentStatement") {
        result = `${formatExpression(statement.variable)} ${statement.op}= ${formatExpression(statement.value)}`
    }
    else if (statementType == 'RepeatStatement') {
		// repeat
		// 	   wait()
		// until game:IsLoaded()

        result = joinStatements('repeat', formatStatementList(statement.body, indent + 1), "\n");
        result = joinStatements(result, 'until', newline);
        result = joinStatements(result, formatExpression(statement.condition, null, indent + 1))

    } else if (statementType == 'FunctionDeclaration') {

        result = (statement.isLocal ? 'local ' : '') + 'function ';
        result += formatExpression(statement.identifier, null, indent);
        result += '(';

        if (statement.parameters.length) {
            each(statement.parameters, function(parameter, needsComma) {
                // `Identifier`s have a `name`, `VarargLiteral`s have a `value`
                result += parameter.name || parameter.value;
                if (needsComma)
					result += ', ';
            });
        }

        result += ')';
        result = joinStatements(result, formatStatementList(statement.body, indent + 1), "\n");
        result = joinStatements(result, end);

    } else if (statementType == 'ForGenericStatement') {
        // see also `ForNumericStatement`

        result = 'for ';

        each(statement.variables, function(variable, needsComma) {
            // The variables in a `ForGenericStatement` are always local
            result += variable.name;
            if (needsComma) 
				result += ', ';
        });

        result += ' in';

        each(statement.iterators, function(iterator, needsComma) {
            result = joinStatements(result, formatExpression(iterator, null, indent));
            if (needsComma)
                result += ', ';
        });

        result = joinStatements(result, `do`);
        result = joinStatements(result, formatStatementList(statement.body, indent + 1), "\n");
        result = joinStatements(result, end);

    } else if (statementType == 'ForNumericStatement') {

        // The variables in a `ForNumericStatement` are always local
        result = 'for ' + statement.variable.name + ' = ';
        result += formatExpression(statement.start, null, indent) + ', ' +
            formatExpression(statement.end, null, indent);

        if (statement.step) {
            result += ', ' + formatExpression(statement.step, null, indent);
        }

        result = joinStatements(result, 'do');
        result = joinStatements(result, formatStatementList(statement.body, indent + 1), "\n");
        result = joinStatements(result, end);

    } else if (statementType == 'LabelStatement')
        result = '::' + statement.label.name + '::';
    else if (statementType == 'GotoStatement')
        result = 'goto ' + statement.label.name;
    else 
        throw TypeError('Unknown statement type: `' + statementType + '`');

    return result;
};

const beautify = (argument) => {
    // `argument` can be a Lua code snippet (string)
    // or a luaparse-compatible AST (object)
    var ast = typeof argument == 'string' ?
        parse(argument) :
        argument;

    return formatStatementList(Array.isArray(ast) ? ast : ast.body);
};

module.exports = beautify