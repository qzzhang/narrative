/**
 * @author Bill Riehl <wjriehl@lbl.gov>
 * @public
 */

(function( $, undefined ) {
    $.KBWidget({
        name: "kbaseDefaultNarrativeInput",
        parent: "kbaseNarrativeInput",
        version: "1.0.0",
        options: {
            loadingImage: "../images/ajax-loader.gif",
        },

        init: function(options) {
            this._super(options);

            this.render();
            return this;
        },

        /**
         * Builds the input div for a function cell, based on the given method object.
         * @param {Object} method - the method being constructed around.
         * @returns {String} an HTML string describing the available parameters for the cell.
         * @private
         */
        render: function() {

            // figure out all types from the method
            var method = this.options.method;
            
            var params = method.properties.parameters;
            var lookupTypes = [];
            for (var p in params) {
                lookupTypes.push(params[p].type);
            }
            this.trigger('dataLoadedQuery.Narrative', [lookupTypes, $.proxy(
                function(objects) {

                    var inputDiv = "<div class='kb-cell-params'><table class='table'>";
                    for (var i=0; i<Object.keys(params).length; i++) {
                        var pid = 'param' + i;
                        var p = params[pid];

                        var input = "";
                        var input_default = (p.default !== "" && p.default !== undefined) ?
                            " placeholder='" + p.default + "'" : "";
                        if (objects[p.type] && objects[p.type].length > 1) {
                            var objList = objects[p.type];
                            objList.sort(function(a, b) {
                                if (a[0] < b[0])
                                    return -1;
                                if (a[0] > b[0])
                                    return 1;
                                return 0;
                            });
                            var datalistUUID = this.genUUID();
                            input = "<input type='text' name='" + pid + "'" + input_default +
                                    " list='" + datalistUUID + "'>" +
                                    "<datalist id='" + datalistUUID + "'>";

                            for (var j=0; j < objects[p.type].length; j++) {
                                input += "<option value='" + objList[j][0] + "'>" + objList[j][0] + "</option>";
                            }
                            input += "</datalist>";
                        }
                        else {
                            input = "<input name='" + pid + "'" + input_default +
                                    " value='' type='text'></input>";
                        }

                        var cellStyle = "border:none; vertical-align:middle;";
                        inputDiv += "<tr style='" + cellStyle + "'>" + 
                                        "<td style='" + cellStyle + "'>" + p.ui_name + "</td>" +
                                        "<td style='" + cellStyle + "'>" + input + "</td>" +
                                        "<td style='" + cellStyle + "'>" + p.description + "</td>" +
                                    "</tr>";
                    }
                    inputDiv += "</table></div>";
                    this.$elem.append(inputDiv);
                },
                this
            )]);
        },

        /**
         * Returns a list of parameters in the order in which the given method
         * requires them.
         * @return {Array} an array of strings - one for each parameter
         * @public
         */
        getParameters: function() {
            var paramList = [];

            $(this.$elem).find("[name^=param]").filter(":input").each(function(key, field) {
                paramList.push(field.value);
            });

            return paramList;
        },

        /**
         * Returns an object representing the state of this widget.
         * In this particular case, it is a list of key-value pairs, like this:
         * { 
         *   'param0' : 'parameter value',
         *   'param1' : 'parameter value'
         * }
         * with one key/value for each parameter in the defined method.
         */
        getState: function() {
            var state = {};

            $(this.$elem).find("[name^=param]").filter(":input").each(function(key, field) {
                state[field.name] = field.value;
            });

            return state;
        },

        /**
         * Adjusts the current set of parameters based on the given state.
         * Doesn't really do a whole lot of type checking yet, but it's assumed that
         * a state will be loaded from an object generated by getState.
         */
        loadState: function(state) {
            if (!state)
                return;

            $(this.$elem).find("[name^=param]").filter(":input").each(function(key, field) {
                var $field = $(field);
                var fieldName = $field.attr("name");

                // If it's a text field, just dump the value in there.
                if ($field.is("input") && $field.attr("type") === "text") {
                    $field.val(state[fieldName]);
                }

                // If it's a select field, do the same... we'll have comboboxen or something,
                // eventually, so I'm just leaving this open for that.
                else if ($field.is("select")) {
                    $field.val(state[fieldName]);
                }
            });
        },

        /**
         * Refreshes the input fields for this widget. I.e. if any of them reference workspace
         * information, those fields get refreshed without altering any other inputs.
         */
        refresh: function() {
            var method = this.options.method;
            var params = method.properties.parameters;
            var lookupTypes = [];
            for (var p in params) {
                lookupTypes.push(params[p].type);
            }

            this.trigger('dataLoadedQuery.Narrative', [lookupTypes, $.proxy(
                function(objects) {
                    // we know from each parameter what each input type is.
                    // we also know how many of each type there is.
                    // so, iterate over all parameters and fulfill cases as below.

                    for (var i=0; i<Object.keys(params).length; i++) {
                        var pid = 'param' + i;
                        var p = params[pid];

                        // we're refreshing, not rendering, so assume that there's an
                        // input with name = pid present.
                        var $input = $($(this.$elem).find("[name=" + pid + "]"));
                        var objList = [];
                        if (objects[p.type] && objects[p.type].length > 0) {
                            objList = objects[p.type];
                            objList.sort(function(a, b) {
                                if (a[0] < b[0]) return -1;
                                if (a[0] > b[0]) return 1;
                                return 0;
                            });
                        }

                        /* down to cases:
                         * 1. (simple) objList is empty, $input doesn't have a list attribute.
                         * -- don't do anything.
                         * 2. objList is empty, $input has a list attribute.
                         * -- no more data exists, so remove that list attribute and the associated datalist element
                         * 3. objList is not empty, $input doesn't have a list attribute.
                         * -- data exists, new datalist needs to be added and linked.
                         * 4. objList is not empty, $input has a list attribute.
                         * -- datalist needs to be cleared and updated.
                         */

                        // case 1 - no data, input is unchanged

                        // case 2 - no data, need to clear input
                        var datalistID = $input.attr('list');
                        if (objList.length == 0 && datalistID) {
                            $(this.$elem.find("#" + datalistID)).remove();
                            $input.removeAttr('list');
                            $input.val("");
                        }

                        // case 3 - data, need new datalist
                        // case 4 - data, need to update existing datalist
                        else if (objList.length > 0) {
                            var $datalist;
                            if (!datalistID) {
                                datalistID = this.genUUID();
                                $input.attr('list', datalistID);
                                $datalist = $('<datalist>')
                                            .attr('id', datalistID);
                                $input.after($datalist);
                            }
                            else {
                                $datalist = $(this.$elem.find("#" + datalistID));
                            }
                            $datalist.empty();
                            for (var j=0; j<objList.length; j++) {
                                $datalist.append($('<option>')
                                                 .attr('value', objList[j][0])
                                                 .append(objList[j][0]));
                            }
                        }
                    }
                },
                this
            )]);
        },

        genUUID: function() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                var r = Math.random()*16|0, v = c == 'x' ? r : (r&0x3|0x8);
                return v.toString(16);
            });
        }

    });

})( jQuery );